"""
The e-Fard integration surface — the "plugs in, does not replace" contract.

Three endpoints, and that is the entire integration burden on the portal:

  POST /v1/integrations/efard/clearance   "May I attest this?"      (Mode A)
  POST /v1/integrations/efard/mirror      "I issued this."          (Mode B)
  GET  /v1/integrations/efard/verify/{id} "Prove this decision."    (evidence)

Failure posture is the important design decision here, and it is deliberately
fail-OPEN with a recorded ``DEGRADED`` outcome. If this service is down, Fard
issuance continues and every request that could not be checked is logged as
DEGRADED so it can be re-checked later.

Why not fail-closed: the Board of Revenue already owns a cautionary tale. When
Punjab made the Green Property Certificate the sole valid instrument, the
alternative was not able to serve every case on day one, and the resulting
disruption forced a partial reversal within weeks and a six-month suspension by
the Assembly. A hard dependency that turns our outage into a province-wide
inability to transact land would repeat that mistake with better intentions.
See docs/03-punjab-postmortem.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...core.hashchain import LedgerEvent
from ...db.models import Clearance, FraudFlag
from ...db.session import get_session
from ...integrations.efard import (
    BaseEfardPort,
    ClearanceResponse,
    SimulatedEfardPort,
)
from ...services import ledger
from ...services import mutations as mut_svc
from ..deps import Principal, Role, require
from ..schemas import ClearanceRequest, ClearanceResponseModel, MirrorRequest

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/efard", tags=["e-Fard integration"])


# --- port singletons -------------------------------------------------------
# Built once per process. In production these are HTTP clients pointed at the
# real portal; in dev they are the deterministic simulators.

_efard_port = None
_nadra_port = None


def get_efard_port(settings: Settings = Depends(get_settings)):
    global _efard_port
    if _efard_port is None:
        from ...integrations.efard import build_efard_port
        _efard_port = build_efard_port(settings)
    return _efard_port


def get_nadra_port(settings: Settings = Depends(get_settings)):
    global _nadra_port
    if _nadra_port is None:
        from ...integrations.nadra import build_nadra_port
        _nadra_port = build_nadra_port(settings)
    return _nadra_port


def _degraded(clearance_id: str, reason: str) -> ClearanceResponseModel:
    return ClearanceResponseModel(
        clearance_id=clearance_id, mutation_id=None, decision="DEGRADED",
        phase="", binding=False, severity="INFO", score=0,
        reasons=[reason], required_actions=[], escalation_role=None,
        expires_at=None, ledger_seq=None, ledger_hash=None,
        config_fingerprint=None, decided_at=datetime.now(timezone.utc),
        verify_url=None, degraded=True, degraded_reason=reason, findings=[],
    )


@router.post("/clearance", response_model=ClearanceResponseModel,
             summary="Pre-issue check — e-Fard calls this BEFORE issuing a Fard")
async def request_clearance(
    body: ClearanceRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    efard=Depends(get_efard_port),
    nadra=Depends(get_nadra_port),
    principal: Principal = Depends(require(Role.SERVICE)),
) -> ClearanceResponseModel:
    """One synchronous call in e-Fard's existing pipeline.

    e-Fard already calls NADRA for biometrics and 1LINK/Easypaisa for payment.
    This is one more call in the same sequence, and it returns in well under a
    second. It does not take over issuance, hold the record, or change who is
    allowed to print a Fard.
    """
    clearance_id = f"CLR-{body.mutation_id}-{int(datetime.now(timezone.utc).timestamp())}"

    payload = body.payload
    if payload is not None:
        efard_mutation = BaseEfardPort._mutation_from_payload({
            **payload, "mutation_id": body.mutation_id})
    else:
        try:
            efard_mutation = await efard.fetch_mutation(body.mutation_id)
        except Exception as exc:
            log.error("e-Fard fetch failed for %s: %s", body.mutation_id, exc)
            await session.rollback()
            return _degraded(clearance_id, f"e-Fard lookup failed: {exc}")

    if efard_mutation is None:
        raise HTTPException(404, f"mutation {body.mutation_id} not found in e-Fard")

    # Districts outside the pilot pass straight through — that is the whole
    # point of a phased rollout. Nobody outside the pilot is slowed down.
    if (efard_mutation.district_code.upper() not in
            [d.upper() for d in settings.pilot_districts]):
        return ClearanceResponseModel(
            clearance_id=clearance_id, mutation_id=body.mutation_id,
            decision="CLEAR", phase="NOT_PILOT", binding=False, severity="INFO",
            score=0, reasons=["District is not in the pilot; pass-through."],
            required_actions=[], escalation_role=None, expires_at=None,
            ledger_seq=None, ledger_hash=None, config_fingerprint=None,
            decided_at=datetime.now(timezone.utc), verify_url=None,
            degraded=False, findings=[],
        )

    try:
        evaluation, rec, _ = await mut_svc.run_clearance(
            session, efard_mutation, settings=settings, nadra=nadra, efard_port=efard)
        await session.commit()
    except Exception as exc:
        log.exception("clearance evaluation failed for %s", body.mutation_id)
        await session.rollback()
        return _degraded(clearance_id, f"rule engine failure: {exc}")

    # Tell e-Fard what we decided (fire-and-forget; it does not gate our reply).
    try:
        await efard.notify_decision(ClearanceResponse(
            clearance_id=rec.clearance_id, decision=rec.decision,
            severity=rec.severity, score=rec.score, reasons=list(rec.reasons or []),
            required_actions=list(rec.required_actions or []), binding=rec.binding,
            phase=rec.phase, ledger_seq=rec.ledger_seq, ledger_hash=rec.ledger_hash,
            verify_url=None, expires_at=rec.expires_at))
    except Exception as exc:  # pragma: no cover
        log.warning("decision callback to e-Fard failed: %s", exc)

    flags = (await session.execute(
        select(FraudFlag).where(FraudFlag.mutation_id == body.mutation_id)
    )).scalars().all()

    return ClearanceResponseModel(
        clearance_id=rec.clearance_id,
        mutation_id=rec.mutation_id,
        decision=rec.decision,
        phase=rec.phase,
        binding=rec.binding,
        severity=rec.severity,
        score=rec.score,
        reasons=list(rec.reasons or []),
        required_actions=list(rec.required_actions or []),
        escalation_role=rec.escalation_role,
        expires_at=rec.expires_at,
        ledger_seq=rec.ledger_seq,
        ledger_hash=rec.ledger_hash,
        config_fingerprint=rec.config_fingerprint,
        decided_at=rec.decided_at,
        verify_url=f"{settings.public_base_url.rstrip('/')}/v1/integrations/efard/"
                   f"verify/{rec.clearance_id}",
        findings=[{
            "rule_id": f.rule_id, "severity": f.severity, "title": f.title,
            "detail": f.detail, "rule_name": f.rule_name,
            "recommended_action": f.recommended_action,
        } for f in flags],
    )


@router.post("/mirror", summary="Post-issue mirroring — e-Fard tells us what it issued")
async def mirror_issued_fard(
    body: MirrorRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.SERVICE)),
):
    """Retrospective audit (Mode B).

    Even before enforcement, mirroring every issued Fard into the chain means we
    can answer "what did this district actually issue last month, and has any of
    it been touched since?" — which is what makes Phase 0 worth running.
    """
    entry = await ledger.append(
        session,
        event_type=LedgerEvent.FARD_MIRRORED,
        entity_type="fard",
        entity_id=body.fard_id,
        actor_id="efard-portal",
        actor_role="SERVICE",
        district_code=body.district_code.upper(),
        payload={
            "mutation_id": body.mutation_id,
            "district": body.district_code.upper(),
            "mouza": body.mouza_code,
            "khasra_no": body.khasra_no,
            "issued_at": (body.issued_at or datetime.now(timezone.utc)).isoformat(),
            "channel": body.channel,
            "document_sha256": body.document_sha256,
            "qr_present": bool(body.qr_payload),
        },
        settings=settings,
        occurred_at=body.issued_at,
    )
    await session.commit()
    return {"status": "mirrored", "fard_id": body.fard_id,
            "ledger_seq": entry.seq, "ledger_hash": entry.entry_hash}


@router.get("/verify/{clearance_id}", summary="Prove a decision (public evidence)")
async def verify_clearance(
    clearance_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """Public, read-only evidence for one clearance decision.

    This is the URL a citizen's QR code or an SMS can point at. It shows the
    decision, the reasons, and the hash of the ledger entry that records it —
    so anyone can independently recompute the chain and confirm it.
    """
    rec = await session.get(Clearance, clearance_id)
    if rec is None:
        raise HTTPException(404, "clearance not found")
    check = await ledger.verify(session, settings, entity_type="clearance",
                                entity_id=clearance_id)
    return {
        "clearance_id": rec.clearance_id,
        "mutation_id": rec.mutation_id,
        "decision": rec.decision,
        "phase": rec.phase,
        "severity": rec.severity,
        "score": rec.score,
        "reasons": rec.reasons,
        "required_actions": rec.required_actions,
        "decided_at": rec.decided_at,
        "ledger_seq": rec.ledger_seq,
        "ledger_hash": rec.ledger_hash,
        "config_fingerprint": rec.config_fingerprint,
        "integrity": {
            "entries_checked": check.entries_checked,
            "intact": check.intact,
            "findings": [{"kind": f.kind.value, "seq": f.seq, "detail": f.detail}
                         for f in check.findings],
        },
    }


@router.get("/demo/mutations", summary="List the simulated e-Fard mutation corpus")
async def demo_mutations(efard=Depends(get_efard_port)):
    """Visible only in simulated mode — used by the demo UI and the docs."""
    if not isinstance(efard, SimulatedEfardPort):
        raise HTTPException(404, "not available in HTTP mode")
    return [{
        "mutation_id": m.mutation_id,
        "district": m.district_code,
        "mouza": m.mouza_code,
        "khasra_no": m.khasra_no,
        "area_kanal": str(m.area_kanal),
        "transaction_type": m.transaction_type,
        "declared_price_pkr": str(m.declared_price_pkr),
        "expected": _expected(m.mutation_id),
    } for m in efard.list_mutations()]


def _expected(mutation_id: str) -> str:
    return {
        "MUT-KP-1001": "CLEAR — clean sale, above DC rate, no prior mutations",
        "MUT-KP-1002": "R1 RED — same khasra mutated twice in a day",
        "MUT-KP-1003": "R2 RED — declared at ~36% of DC value (benami)",
        "MUT-KP-1004": "R3 RED — seller recorded deceased by NADRA",
        "MUT-KP-1005": "R4 RED — POA attorney never biometrically verified",
        "MUT-KP-1006": "R5 AMBER — clan/mouza differs from the latha",
        "MUT-KP-1007": "R6 RED — 20 kanal sale with no ground verification at all",
    }.get(mutation_id, "")
