"""
efard.py — the seam between this system and the e-Fard portal / LRMIS.

**We do not replace e-Fard. We are a filter it calls, and a tape recorder it
writes to.**

Two integration modes, both implemented:

MODE A — PRE-ISSUE INTERCEPTION (primary, and the only mode that *prevents*
         anything)
    e-Fard already asks NADRA for a biometric check and 1LINK/Easypaisa for
    payment before it prints a Fard. We add one more synchronous call in that
    same pipeline::

        e-Fard ──► POST /v1/integrations/efard/clearance ──► CLEAR | HOLD | BLOCK
                                   │
                                   └─► every call is written to the hash chain

    Contract: if we are unreachable, e-Fard proceeds and we record a
    ``DEGRADED`` outcome. A hard fail-closed would let our downtime become a
    province-wide outage of Fard issuance — precisely the failure mode Punjab
    hit when the Green Certificate replaced Fard faster than it could serve
    every case. See docs/03-punjab-postmortem.md.

MODE B — POST-ISSUE MIRRORING (retrospective audit)
    For mutations already attested, or for districts where the pre-issue call
    is not yet wired in, we poll/mirror what e-Fard issued and write it to the
    chain as ``FARD_MIRRORED``. This gives us a tamper-evident history of what
    was issued even before enforcement begins, which is what makes Phase 0
    useful.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Data contracts (mirrors the shapes e-Fard already holds)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class EfardMutation:
    """Normalised view of a mutation coming out of LRMIS.

    Field names follow the revenue vocabulary (khewat / khasra / mouza) rather
    than a generic 'parcel' abstraction, because the people reading these logs
    are Patwaris and Tehsildars.
    """
    mutation_id: str
    district_code: str
    tehsil_code: str
    mouza_code: str
    mouza_name: str = ""
    khewat_no: str | None = None
    khasra_no: str = ""
    area_kanal: Decimal = Decimal("0")
    transaction_type: str = "SALE"
    declared_price_pkr: Decimal = Decimal("0")
    patwari_id: str | None = None
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sellers: list[dict[str, Any]] = field(default_factory=list)
    buyers: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EfardFard:
    """A Fard that e-Fard issued."""
    fard_id: str
    mutation_id: str | None
    district_code: str
    mouza_code: str
    khasra_no: str
    issued_at: datetime
    qr_payload: str
    document_sha256: str | None = None
    channel: str = "PORTAL"          # PORTAL | E_SAHULAT | DASTAK
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClearanceResponse:
    """The answer we send back. Small, fast, and unambiguous."""
    clearance_id: str
    decision: str                    # CLEAR | HOLD | BLOCK | ADVISORY | DEGRADED
    severity: str
    score: int
    reasons: list[str]
    required_actions: list[dict[str, Any]]
    binding: bool
    phase: str
    ledger_seq: int | None
    ledger_hash: str | None
    verify_url: str | None
    expires_at: datetime | None
    degraded: bool = False
    degraded_reason: str | None = None


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------

@runtime_checkable
class EfardPort(Protocol):
    """Everything this system needs from e-Fard. Nothing more.

    Keeping the surface this small is deliberate: it is the difference between
    an anti-corruption overlay that a government can adopt in a quarter and a
    land-records rewrite that takes a decade.
    """

    async def fetch_mutation(self, mutation_id: str) -> EfardMutation | None: ...

    async def fetch_khasra_history(self, district: str, mouza: str, khasra: str,
                                   since: datetime) -> list[EfardMutation]: ...

    async def fetch_recent_fards(self, since: datetime,
                                 district: str | None = None) -> list[EfardFard]: ...

    async def verify_qr(self, qr_payload: str) -> dict[str, Any] | None: ...

    async def notify_decision(self, clearance: ClearanceResponse) -> bool: ...


class BaseEfardPort:
    """Shared helpers for concrete ports.

    Not an ABC: it declares no abstract surface of its own. The contract lives
    in EfardPort, which is a Protocol, so a port can be swapped without
    inheriting anything.
    """

    @staticmethod
    def _mutation_from_payload(data: dict[str, Any]) -> EfardMutation:
        def dec(v: Any) -> Decimal:
            try:
                return Decimal(str(v))
            except Exception:
                return Decimal("0")

        def dt(v: Any) -> datetime:
            if isinstance(v, datetime):
                return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            try:
                parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.now(timezone.utc)

        return EfardMutation(
            mutation_id=str(data.get("mutation_id") or data.get("id") or ""),
            district_code=str(data.get("district_code") or data.get("district") or "").upper(),
            tehsil_code=str(data.get("tehsil_code") or data.get("tehsil") or ""),
            mouza_code=str(data.get("mouza_code") or data.get("mouza") or ""),
            mouza_name=str(data.get("mouza_name") or ""),
            khewat_no=(str(data["khewat_no"]) if data.get("khewat_no") else None),
            khasra_no=str(data.get("khasra_no") or data.get("khasra") or ""),
            area_kanal=dec(data.get("area_kanal") or 0),
            transaction_type=str(data.get("transaction_type") or "SALE").upper(),
            declared_price_pkr=dec(data.get("declared_price_pkr")
                                   or data.get("consideration") or 0),
            patwari_id=(str(data["patwari_id"]) if data.get("patwari_id") else None),
            proposed_at=dt(data.get("proposed_at") or data.get("created_at")),
            sellers=list(data.get("sellers") or []),
            buyers=list(data.get("buyers") or []),
            raw=data,
        )


# --------------------------------------------------------------------------
# HTTP implementation (production)
# --------------------------------------------------------------------------

class HttpEfardPort(BaseEfardPort, EfardPort):
    """Talks to the real portal. mTLS / API key are configured by environment.

    The endpoint paths are placeholders — the KP Board of Revenue publishes the
    real ones to integrators. They live in config, not in code, so nobody has
    to patch a Python file to point at staging.
    """

    def __init__(self, *, base_url: str, api_key: str = "", timeout_s: float = 8.0,
                 client_cert: str | None = None, client_key: str | None = None,
                 verify_tls: bool = True) -> None:
        if not base_url:
            raise ValueError("efard base_url is required for HTTP mode")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.client_cert = client_cert
        self.client_key = client_key
        self.verify_tls = verify_tls

    def _client(self):
        import httpx

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        cert = (self.client_cert, self.client_key) if self.client_cert else None
        return httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                 timeout=self.timeout_s, cert=cert,
                                 verify=self.verify_tls)

    async def fetch_mutation(self, mutation_id: str) -> EfardMutation | None:
        async with self._client() as c:
            r = await c.get(f"/api/mutations/{mutation_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return self._mutation_from_payload(r.json())

    async def fetch_khasra_history(self, district: str, mouza: str, khasra: str,
                                   since: datetime) -> list[EfardMutation]:
        async with self._client() as c:
            r = await c.get("/api/mutations", params={
                "district": district, "mouza": mouza, "khasra": khasra,
                "since": since.isoformat(),
            })
            r.raise_for_status()
            data = r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            return [self._mutation_from_payload(i) for i in items]

    async def fetch_recent_fards(self, since: datetime,
                                 district: str | None = None) -> list[EfardFard]:
        async with self._client() as c:
            r = await c.get("/api/fards", params={
                "since": since.isoformat(), **({"district": district} if district else {})})
            r.raise_for_status()
            data = r.json()
            items = data.get("items", data) if isinstance(data, dict) else data
            out = []
            for i in items:
                issued = i.get("issued_at")
                out.append(EfardFard(
                    fard_id=str(i.get("fard_id") or i.get("id")),
                    mutation_id=(str(i["mutation_id"]) if i.get("mutation_id") else None),
                    district_code=str(i.get("district_code", "")).upper(),
                    mouza_code=str(i.get("mouza_code", "")),
                    khasra_no=str(i.get("khasra_no", "")),
                    issued_at=(datetime.fromisoformat(str(issued).replace("Z", "+00:00"))
                               if issued else datetime.now(timezone.utc)),
                    qr_payload=str(i.get("qr_payload", "")),
                    document_sha256=i.get("document_sha256"),
                    channel=str(i.get("channel", "PORTAL")).upper(),
                    raw=i,
                ))
            return out

    async def verify_qr(self, qr_payload: str) -> dict[str, Any] | None:
        async with self._client() as c:
            r = await c.post("/api/verify-qr", json={"qr": qr_payload})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def notify_decision(self, clearance: ClearanceResponse) -> bool:
        async with self._client() as c:
            r = await c.post("/api/mutations/clearance", json={
                "clearance_id": clearance.clearance_id,
                "decision": clearance.decision,
                "binding": clearance.binding,
                "ledger_hash": clearance.ledger_hash,
            })
            return r.status_code < 400


# --------------------------------------------------------------------------
# Simulator (no credentials required — used for dev, demo, CI and training)
# --------------------------------------------------------------------------

class SimulatedEfardPort(BaseEfardPort, EfardPort):
    """A deterministic in-memory stand-in for the portal.

    It reproduces the *shapes* the real API returns, including a handful of
    recognisable fraud patterns (a rapid re-mutation chain, a benami
    undervaluation, a dead seller, an unverified POA) so the rules engine can
    be demonstrated end to end without touching production data.
    """

    def __init__(self, seed: bool = True) -> None:
        self._mutations: dict[str, EfardMutation] = {}
        self._history: list[EfardMutation] = []
        self._fards: list[EfardFard] = []
        self._decisions: list[dict[str, Any]] = []
        if seed:
            self._seed()

    # -- seeding -----------------------------------------------------------
    def _seed(self) -> None:
        now = datetime.now(timezone.utc)

        # A prior mutation on khasra 512, 12 days ago. This is the one that
        # makes MUT-KP-1002 the *second* transfer of the same survey number
        # inside 30 days — the classic laundering chain.
        self._history.append(EfardMutation(
            mutation_id="MUT-KP-0998", district_code="PESHAWAR", tehsil_code="PESHAWAR",
            mouza_code="MZ-PSH-001", mouza_name="Landi Arbab", khewat_no="18",
            khasra_no="512", area_kanal=Decimal("4"), transaction_type="SALE",
            declared_price_pkr=Decimal("4100000"), patwari_id="PAT-PSH-07",
            proposed_at=now - timedelta(days=12),
            sellers=[{"name": "Hidayatullah", "cnic": "1730111000001",
                      "father_name": "Mian Gul", "qaum": "Khattak", "age": 49,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Fazal Rahim", "cnic": "1730155512345",
                     "father_name": "Niaz Muhammad", "qaum": "Afridi", "age": 55,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        ))

        # Clean transaction — should come back CLEAR.
        self._mutations["MUT-KP-1001"] = EfardMutation(
            mutation_id="MUT-KP-1001", district_code="PESHAWAR", tehsil_code="PESHAWAR",
            mouza_code="MZ-PSH-001", mouza_name="Landi Arbab", khewat_no="12",
            khasra_no="441", area_kanal=Decimal("8"), transaction_type="SALE",
            declared_price_pkr=Decimal("9600000"), patwari_id="PAT-PSH-07",
            proposed_at=now - timedelta(hours=2),
            sellers=[{"name": "Gul Muhammad Khan", "cnic": "1730112345671",
                      "father_name": "Sher Dil Khan", "qaum": "Khattak", "age": 62,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Asmatullah", "cnic": "1730198765432",
                     "father_name": "Bahadar Shah", "qaum": "Khattak", "age": 41,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # Rapid re-mutation chain — R1 RED.
        self._mutations["MUT-KP-1002"] = EfardMutation(
            mutation_id="MUT-KP-1002", district_code="PESHAWAR", tehsil_code="PESHAWAR",
            mouza_code="MZ-PSH-001", mouza_name="Landi Arbab", khewat_no="18",
            khasra_no="512", area_kanal=Decimal("4"), transaction_type="SALE",
            declared_price_pkr=Decimal("4200000"), patwari_id="PAT-PSH-07",
            proposed_at=now - timedelta(hours=5),
            sellers=[{"name": "Fazal Rahim", "cnic": "1730155512345",
                      "father_name": "Niaz Muhammad", "qaum": "Afridi", "age": 55,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Imran Ali", "cnic": "1730166678901",
                     "father_name": "Zahid Ali", "qaum": "Afridi", "age": 38,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # Benami undervaluation — R2 RED.
        self._mutations["MUT-KP-1003"] = EfardMutation(
            mutation_id="MUT-KP-1003", district_code="MARDAN", tehsil_code="MARDAN",
            mouza_code="MZ-MRD-002", mouza_name="Bakhshali", khewat_no="44",
            khasra_no="1902", area_kanal=Decimal("10"), transaction_type="SALE",
            declared_price_pkr=Decimal("2500000"), patwari_id="PAT-MRD-03",
            proposed_at=now - timedelta(hours=1),
            sellers=[{"name": "Zarin Gul", "cnic": "1710177788899",
                      "father_name": "Amir Gul", "qaum": "Yousafzai", "age": 58,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Rashid Khan", "cnic": "1710188899900",
                     "father_name": "Lal Khan", "qaum": "Yousafzai", "age": 44,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # Dead seller — R3 RED.
        self._mutations["MUT-KP-1004"] = EfardMutation(
            mutation_id="MUT-KP-1004", district_code="SWAT", tehsil_code="BABUZAI",
            mouza_code="MZ-SWT-001", mouza_name="Saidu Sharif", khewat_no="7",
            khasra_no="88", area_kanal=Decimal("6"), transaction_type="SALE",
            declared_price_pkr=Decimal("5400000"), patwari_id="PAT-SWT-11",
            proposed_at=now - timedelta(hours=3),
            sellers=[{"name": "Mir Afzal Khan", "cnic": "1720111122233",
                      "father_name": "Bahram Khan", "qaum": "Yousafzai", "age": 79,
                      "nadra_status": "DECEASED", "biometric_verified": False}],
            buyers=[{"name": "Samiullah", "cnic": "1720122233344",
                     "father_name": "Amanullah", "qaum": "Yousafzai", "age": 35,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # Unverified power of attorney — R4 RED.
        self._mutations["MUT-KP-1005"] = EfardMutation(
            mutation_id="MUT-KP-1005", district_code="PESHAWAR", tehsil_code="PESHAWAR",
            mouza_code="MZ-PSH-002", mouza_name="Mattani", khewat_no="91",
            khasra_no="330", area_kanal=Decimal("12"), transaction_type="SALE",
            declared_price_pkr=Decimal("18000000"), patwari_id="PAT-PSH-12",
            proposed_at=now - timedelta(hours=6),
            sellers=[{"name": "Nasreen Bibi", "cnic": "1730144455566",
                      "father_name": "Ghulam Nabi", "qaum": "Mohmand", "age": 67,
                      "nadra_status": "LIVE", "biometric_verified": True,
                      "acting_via_poa": True, "poa_biometric_verified": False,
                      "poa_registered": True, "poa_attorney_cnic": "1730155566677"}],
            buyers=[{"name": "Javed Iqbal", "cnic": "1730166677788",
                     "father_name": "Muhammad Iqbal", "qaum": "Mohmand", "age": 47,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # Caste/mouza drift vs latha — R5 AMBER.
        self._mutations["MUT-KP-1006"] = EfardMutation(
            mutation_id="MUT-KP-1006", district_code="MARDAN", tehsil_code="TAKHT-I-BHAI",
            mouza_code="MZ-MRD-003", mouza_name="Takht-i-Bhai", khewat_no="23",
            khasra_no="77", area_kanal=Decimal("5"), transaction_type="INHERITANCE",
            declared_price_pkr=Decimal("0"), patwari_id="PAT-MRD-09",
            proposed_at=now - timedelta(hours=8),
            sellers=[{"name": "Bakht Zamina", "cnic": "1710133344455",
                      "father_name": "Rozi Khan", "qaum": "Utmanzai", "age": 71,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Khalid Usman", "cnic": "1710144455566",
                     "father_name": "Usman Ghani", "qaum": "Khattak", "age": 39,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

        # A forged desktop mutation with no ground verification — R6 RED.
        self._mutations["MUT-KP-1007"] = EfardMutation(
            mutation_id="MUT-KP-1007", district_code="SWAT", tehsil_code="MATTA",
            mouza_code="MZ-SWT-002", mouza_name="Matta", khewat_no="55",
            khasra_no="1401", area_kanal=Decimal("20"), transaction_type="SALE",
            declared_price_pkr=Decimal("30000000"), patwari_id="PAT-SWT-04",
            proposed_at=now - timedelta(minutes=40),
            sellers=[{"name": "Sher Zaman", "cnic": "1720155566677",
                      "father_name": "Zaman Ali", "qaum": "Yousafzai", "age": 51,
                      "nadra_status": "LIVE", "biometric_verified": True}],
            buyers=[{"name": "Tariq Aziz", "cnic": "1720166677788",
                     "father_name": "Azizullah", "qaum": "Yousafzai", "age": 42,
                     "nadra_status": "LIVE", "biometric_verified": True}],
        )

    # -- port methods ------------------------------------------------------
    async def fetch_mutation(self, mutation_id: str) -> EfardMutation | None:
        return self._mutations.get(mutation_id)

    async def fetch_khasra_history(self, district: str, mouza: str, khasra: str,
                                   since: datetime) -> list[EfardMutation]:
        pool = list(self._mutations.values()) + self._history
        return [m for m in pool
                if m.district_code == district and m.mouza_code == mouza
                and m.khasra_no == khasra and m.proposed_at >= since]

    async def fetch_recent_fards(self, since: datetime,
                                 district: str | None = None) -> list[EfardFard]:
        return [f for f in self._fards
                if f.issued_at >= since and (not district or f.district_code == district)]

    async def verify_qr(self, qr_payload: str) -> dict[str, Any] | None:
        digest = hashlib.sha256(qr_payload.encode("utf-8")).hexdigest()
        return {"valid": True, "qr_sha256": digest,
                "note": "simulated e-Fard QR verification"}

    async def notify_decision(self, clearance: ClearanceResponse) -> bool:
        self._decisions.append({
            "clearance_id": clearance.clearance_id,
            "decision": clearance.decision,
            "ledger_hash": clearance.ledger_hash,
        })
        return True

    # -- helpers for the demo harness --------------------------------------
    def add_mutation(self, m: EfardMutation) -> None:
        self._mutations[m.mutation_id] = m

    def list_mutations(self) -> list[EfardMutation]:
        return sorted(self._mutations.values(), key=lambda m: m.mutation_id)

    def record_fard(self, f: EfardFard) -> None:
        self._fards.append(f)

    @property
    def decisions(self) -> list[dict[str, Any]]:
        return list(self._decisions)


def build_efard_port(settings) -> EfardPort:
    """Factory — the only place that knows which port implementation to use."""
    if settings.efard_mode == "http" and settings.efard_base_url:
        return HttpEfardPort(base_url=settings.efard_base_url,
                             api_key=settings.efard_api_key,
                             timeout_s=settings.efard_timeout_s)
    log.info("e-Fard integration running in SIMULATED mode (set KLRI_EFARD_MODE=http "
             "and KLRI_EFARD_BASE_URL to call the real portal)")
    return SimulatedEfardPort()
