"""
nadra.py — CNIC / life-status / biometric verification.

e-Fard already uses NADRA biometric verification to decide *who may download a
Fard*. This module asks a different question: **is the person named on this
mutation alive, and are they who the record says they are?** That check (Rule 3)
is what stands between a dead owner's land and whoever notices first.

Privacy
-------
We never store a CNIC in the clear. Everything persisted is a salted hash
(``cnic_hash``), and anything emitted by the API is masked. The hash is enough
to link records and to detect repeat parties; it is not enough to reconstruct
an identity from our database if it leaks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


class VerificationOutcome(str, Enum):
    LIVE = "LIVE"
    DECEASED = "DECEASED"
    NOT_FOUND = "NOT_FOUND"
    BLOCKED = "BLOCKED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(slots=True)
class NadraParty:
    cnic_hash: str
    outcome: VerificationOutcome
    age: int | None = None
    name_match: bool | None = None
    father_name_match: bool | None = None
    biometric_done: bool = False
    checked_at: datetime = None  # type: ignore[assignment]
    source: str = "SIMULATED"


def cnic_hash(cnic: str, pepper: str) -> str:
    """Salted, keyed hash of a CNIC. Deterministic, non-reversible, linkable."""
    digits = re.sub(r"\D", "", cnic or "")
    if not digits:
        return ""
    return hmac.new(pepper.encode("utf-8"), digits.encode("utf-8"), hashlib.sha256).hexdigest()


def normalise_cnic(cnic: str) -> str:
    return re.sub(r"\D", "", cnic or "")


def cnic_checksum_ok(cnic: str) -> bool:
    """Shape check only. NADRA owns the real validation; this just catches typos."""
    return len(normalise_cnic(cnic)) == 13


@runtime_checkable
class NadraPort(Protocol):
    async def verify_party(self, cnic: str, name: str | None = None,
                           father_name: str | None = None) -> NadraParty: ...

    async def verify_biometric(self, cnic: str, biometric_token: str) -> bool: ...

    async def bulk_life_status(self, cnic_hashes: list[str]) -> dict[str, VerificationOutcome]: ...


class SimulatedNadraPort:
    """Deterministic stand-in.

    The death register is seeded from the demo data so the "seller is deceased"
    path is reproducible. Age is derived from the CNIC's embedded birth year
    where present, which is how a real integration would sanity-check it too.
    """

    def __init__(self, pepper: str = "dev-nadra-pepper") -> None:
        self.pepper = pepper
        # CNICs known to be deceased in the demo corpus.
        self._deceased = {"1720111122233"}
        self._blocked: set[str] = set()
        self._calls: list[dict[str, str]] = []

    async def verify_party(self, cnic: str, name: str | None = None,
                           father_name: str | None = None) -> NadraParty:
        self._calls.append({"cnic": cnic[:5] + "*" * 8, "name": name or ""})
        digits = normalise_cnic(cnic)
        outcome = VerificationOutcome.UNVERIFIED
        if not digits:
            outcome = VerificationOutcome.NOT_FOUND
        elif digits in self._deceased:
            outcome = VerificationOutcome.DECEASED
        elif digits in self._blocked:
            outcome = VerificationOutcome.BLOCKED
        elif len(digits) == 13:
            outcome = VerificationOutcome.LIVE
        else:
            outcome = VerificationOutcome.NOT_FOUND

        # NADRA CNIC encodes birth year in digits 7-8 (1960-2059 heuristic).
        age = None
        if len(digits) == 13:
            yy = int(digits[6:8])
            birth_year = 2000 + yy if yy < 60 else 1900 + yy
            now_year = datetime.now(timezone.utc).year
            age = max(0, min(120, now_year - birth_year))

        return NadraParty(cnic_hash=cnic_hash(digits, self.pepper), outcome=outcome,
                          age=age, name_match=None, father_name_match=None,
                          biometric_done=False, checked_at=datetime.now(timezone.utc),
                          source="SIMULATED")

    async def verify_biometric(self, cnic: str, biometric_token: str) -> bool:
        return bool(biometric_token) and cnic_checksum_ok(cnic)

    async def bulk_life_status(self, cnic_hashes: list[str]) -> dict[str, VerificationOutcome]:
        return {h: VerificationOutcome.UNVERIFIED for h in cnic_hashes}


class HttpNadraPort:
    """Real NADRA integration stub.

    The endpoints below are shape-accurate placeholders; the actual contract is
    issued by NADRA under the existing e-Sahulat agreement that e-Fard already
    uses. Note the retry/timeout posture: NADRA being slow must degrade to
    UNVERIFIED (an AMBER flag for a human to resolve), never to LIVE.
    """

    def __init__(self, *, base_url: str, api_key: str = "", pepper: str = "",
                 timeout_s: float = 8.0) -> None:
        if not base_url:
            raise ValueError("nadra base_url is required for HTTP mode")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.pepper = pepper or api_key or "nadra"
        self.timeout_s = timeout_s

    def _client(self):
        import httpx

        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return httpx.AsyncClient(base_url=self.base_url, headers=headers,
                                 timeout=self.timeout_s)

    async def verify_party(self, cnic: str, name: str | None = None,
                           father_name: str | None = None) -> NadraParty:
        async with self._client() as c:
            r = await c.post("/v1/verify", json={
                "cnic": normalise_cnic(cnic), "name": name, "father_name": father_name})
            if r.status_code == 404:
                outcome = VerificationOutcome.NOT_FOUND
                age = None
            else:
                r.raise_for_status()
                data = r.json()
                outcome = VerificationOutcome(str(data.get("status", "UNVERIFIED")).upper())
                age = data.get("age")
        return NadraParty(cnic_hash=cnic_hash(cnic, self.pepper), outcome=outcome, age=age,
                          checked_at=datetime.now(timezone.utc), source="NADRA_HTTP")

    async def verify_biometric(self, cnic: str, biometric_token: str) -> bool:
        async with self._client() as c:
            r = await c.post("/v1/biometric/verify",
                             json={"cnic": normalise_cnic(cnic), "token": biometric_token})
            r.raise_for_status()
            return bool(r.json().get("verified"))

    async def bulk_life_status(self, cnic_hashes: list[str]) -> dict[str, VerificationOutcome]:
        async with self._client() as c:
            r = await c.post("/v1/life-status/bulk", json={"cnic_hashes": cnic_hashes})
            r.raise_for_status()
            return {k: VerificationOutcome(str(v).upper()) for k, v in r.json().items()}


def build_nadra_port(settings) -> NadraPort:
    if settings.nadra_mode == "http" and settings.nadra_base_url:
        return HttpNadraPort(base_url=settings.nadra_base_url,
                             api_key=settings.nadra_api_key)
    log.info("NADRA integration running in SIMULATED mode")
    return SimulatedNadraPort(pepper=settings.ledger_signing_key)
