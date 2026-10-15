"""Fraud flags: the review queue, and the override path that makes dual-run honest."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...core.hashchain import LedgerEvent
from ...db.models import Clearance, FlagStatus, FraudFlag
from ...db.session import get_session
from ...services import ledger
from ..deps import Principal, Role, require
from ..schemas import FlagReview, FraudFlagOut

router = APIRouter(prefix="/fraud", tags=["fraud flags"])


@router.get("/flags", response_model=list[FraudFlagOut], summary="The review queue")
async def list_flags(
    district_code: str | None = None,
    severity: str | None = Query(None, pattern="^(RED|AMBER|INFO)$"),
    rule_id: str | None = None,
    status: str | None = None,
    khasra_no: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    stmt = select(FraudFlag).order_by(FraudFlag.raised_at.desc()).limit(limit)
    if district_code:
        stmt = stmt.where(FraudFlag.district_code == district_code.upper())
    if severity:
        stmt = stmt.where(FraudFlag.severity == severity)
    if rule_id:
        stmt = stmt.where(FraudFlag.rule_id == rule_id)
    if status:
        stmt = stmt.where(FraudFlag.status == status)
    if khasra_no:
        stmt = stmt.where(FraudFlag.khasra_no == khasra_no)
    rows = (await session.execute(stmt)).scalars().all()
    return [FraudFlagOut.model_validate(r) for r in rows]


@router.get("/flags/{flag_id}", response_model=FraudFlagOut)
async def get_flag(
    flag_id: int,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    row = await session.get(FraudFlag, flag_id)
    if row is None:
        raise HTTPException(404, "flag not found")
    return FraudFlagOut.model_validate(row)


@router.patch("/flags/{flag_id}", response_model=FraudFlagOut,
              summary="Review a flag — DISMISSED requires a written reason")
async def review_flag(
    flag_id: int,
    body: FlagReview,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.TEHSILDAR)),
):
    """Closing a flag is itself a recordable act.

    The thing that destroys anti-fraud systems is not false positives; it is
    silent overrides. If an officer can wave a flag away without writing why,
    then within a year the queue is empty and nothing has improved. So the note
    is mandatory, it is written to the chain, and it names the officer.
    """
    row = await session.get(FraudFlag, flag_id)
    if row is None:
        raise HTTPException(404, "flag not found")

    row.status = FlagStatus(body.status)
    row.reviewed_by = principal.subject
    row.reviewed_at = datetime.now(timezone.utc)
    row.review_note = body.note
    await session.flush()

    await ledger.append(
        session,
        event_type=(LedgerEvent.FRAUD_FLAG_CLEARED
                    if body.status in ("DISMISSED",) else LedgerEvent.OFFICER_ACTION),
        entity_type="fraud_flag",
        entity_id=str(flag_id),
        actor_id=principal.subject,
        actor_role=principal.role.value,
        district_code=row.district_code,
        payload={
            "flag_id": flag_id, "rule_id": row.rule_id, "severity": row.severity,
            "mutation_id": row.mutation_id, "new_status": body.status,
            "note": body.note,
        },
        settings=settings,
    )
    await session.commit()
    return FraudFlagOut.model_validate(row)


@router.post("/clearances/{clearance_id}/override",
             summary="Officer proceeds despite a HOLD — recorded, never silent")
async def override_clearance(
    clearance_id: str,
    reason: str = Query(..., min_length=15,
                        description="Mandatory. An override without a written reason "
                                    "is indistinguishable from corruption."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.TEHSILDAR)),
):
    """The dual-run escape hatch.

    In Phase 1 the manual register is still the legal system of record, so an
    officer *can* proceed against our advice. What they cannot do is proceed
    invisibly: the override is written to the chain with their name, the reason,
    and the flags they overrode. At Phase 2 the same data tells the Board of
    Revenue which circles need attention.
    """
    rec = await session.get(Clearance, clearance_id)
    if rec is None:
        raise HTTPException(404, "clearance not found")
    if rec.phase in ("PHASE_2_ENFORCE", "PHASE_3_LEGAL") and rec.decision == "BLOCK":
        raise HTTPException(
            409, "This clearance is a binding BLOCK in an enforcement phase. "
                 "Overrides require Board of Revenue approval, not a Tehsildar.")

    rec.overridden_by = principal.subject
    rec.override_reason = reason
    await session.flush()

    entry = await ledger.append(
        session,
        event_type=LedgerEvent.OFFICER_ACTION,
        entity_type="clearance_override",
        entity_id=clearance_id,
        actor_id=principal.subject,
        actor_role=principal.role.value,
        payload={
            "mutation_id": rec.mutation_id,
            "decision_overridden": rec.decision,
            "severity": rec.severity,
            "score": rec.score,
            "reasons": rec.reasons,
            "override_reason": reason,
            "phase": rec.phase,
        },
        settings=settings,
    )
    await session.commit()
    return {
        "clearance_id": clearance_id,
        "overridden_by": principal.subject,
        "override_reason": reason,
        "ledger_seq": entry.seq,
        "ledger_hash": entry.entry_hash,
        "note": ("Recorded permanently. This override will appear in the officer's "
                 "audit history and in the district override report."),
    }


@router.get("/summary", summary="Flag counts by rule, severity, district and status")
async def summary(
    district_code: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    base = select(FraudFlag)
    if district_code:
        base = base.where(FraudFlag.district_code == district_code.upper())

    by_rule = dict((await session.execute(
        base.add_columns(FraudFlag.rule_id, func.count())
        .group_by(FraudFlag.rule_id))).all())
    by_sev = dict((await session.execute(
        base.add_columns(FraudFlag.severity, func.count())
        .group_by(FraudFlag.severity))).all())
    by_status = dict((await session.execute(
        base.add_columns(FraudFlag.status, func.count())
        .group_by(FraudFlag.status))).all())
    by_district = dict((await session.execute(
        select(FraudFlag.district_code, func.count())
        .group_by(FraudFlag.district_code))).all())

    return {
        "by_rule": by_rule,
        "by_severity": by_sev,
        "by_status": {k.value if hasattr(k, "value") else k: v for k, v in by_status.items()},
        "by_district": by_district,
    }
