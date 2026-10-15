"""District dashboard: what a DC or Tehsildar looks at on Monday morning."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...db.geo import postgis_available
from ...db.models import FraudFlag, GroundVerification, Mutation
from ...db.session import get_session
from ...services import ledger, ownership
from ..deps import Principal, Role, require
from ..schemas import DashboardOut

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOut, summary="District rollup")
async def dashboard(
    district_code: str | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    chain = await ledger.verify(session, settings)

    flag_q = select(FraudFlag.rule_id, func.count()).group_by(FraudFlag.rule_id)
    sev_q = select(FraudFlag.severity, func.count()).group_by(FraudFlag.severity)
    status_q = select(Mutation.status, func.count()).group_by(Mutation.status)
    if district_code:
        flag_q = flag_q.where(FraudFlag.district_code == district_code.upper())
        sev_q = sev_q.where(FraudFlag.district_code == district_code.upper())
        status_q = status_q.where(Mutation.district_code == district_code.upper())

    by_rule = dict((await session.execute(flag_q)).all())
    by_sev = dict((await session.execute(sev_q)).all())
    by_status = dict((await session.execute(status_q)).all())

    ground_q = select(GroundVerification.status, func.count())
    ground_q = ground_q.group_by(GroundVerification.status)
    g_status = dict((await session.execute(ground_q)).all())
    avg_trust = (await session.execute(
        select(func.avg(GroundVerification.trust_score)))).scalar_one_or_none()

    return DashboardOut(
        phase=settings.phase,
        pilot_districts=settings.pilot_districts,
        chain={
            "entries": chain.entries_checked,
            "intact": chain.intact,
            "head_hash": chain.head_hash,
            "findings": [{"kind": f.kind.value, "seq": f.seq, "detail": f.detail}
                         for f in chain.findings],
        },
        flags_by_rule=by_rule,
        flags_by_severity=by_sev,
        ground={
            "by_status": {str(k.value if hasattr(k, "value") else k): v
                          for k, v in g_status.items()},
            "avg_trust_score": round(float(avg_trust), 1) if avg_trust else None,
        },
        mutations_by_status={str(k.value if hasattr(k, "value") else k): v
                             for k, v in by_status.items()},
        oversubscribed_khasras=await ownership.oversubscribed_khasras(
            session, district=district_code, limit=20),
        geo_engine="postgis" if await postgis_available(session) else "shapely",
        generated_at=datetime.now(timezone.utc),
    )


@router.get("/tamper-report", summary="Human-readable integrity report")
async def tamper_report(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.AUDITOR)),
):
    """The report a court or an audit team asks for.

    States plainly: how many entries, whether the chain verifies, and if not,
    exactly which sequence numbers are wrong and in what way.
    """
    chain = await ledger.verify(session, settings)
    lines = [
        "KP LAND RECORD INTEGRITY — LEDGER VERIFICATION REPORT",
        f"Generated : {datetime.now(timezone.utc).isoformat()}",
        f"Chain     : {chain.chain_key}",
        f"Entries   : {chain.entries_checked} (seq {chain.first_seq}..{chain.last_seq})",
        f"Head hash : {chain.head_hash}",
        f"Merkle    : {chain.recomputed_root}",
        f"Result    : {'INTACT' if chain.intact else 'TAMPERING DETECTED'}",
        "",
    ]
    if chain.findings:
        lines.append("FINDINGS")
        for f in chain.findings:
            lines.append(f"  [{f.kind.value}] seq={f.seq}: {f.detail}")
    else:
        lines.append("No discrepancies. Every entry's hash recomputes correctly, "
                     "every link matches its predecessor, and no sequence gaps "
                     "were found.")
    return {"report": "\n".join(lines), "intact": chain.intact,
            "entries": chain.entries_checked}
