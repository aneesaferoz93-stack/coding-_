"""Mutations, ownership chains and the double-sale audit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...db.models import Mutation
from ...db.session import get_session
from ...services import mutations as mut_svc
from ...services import ownership
from ..deps import Principal, Role, require
from ..schemas import MutationOut, ShareOut

router = APIRouter(prefix="/mutations", tags=["mutations"])


@router.get("", response_model=list[MutationOut], summary="List mirrored mutations")
async def list_mutations(
    district_code: str | None = None,
    mouza_code: str | None = None,
    status: str | None = None,
    khasra_no: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    stmt = select(Mutation).order_by(Mutation.proposed_at.desc()).limit(limit)
    if district_code:
        stmt = stmt.where(Mutation.district_code == district_code.upper())
    if mouza_code:
        stmt = stmt.where(Mutation.mouza_code == mouza_code)
    if status:
        stmt = stmt.where(Mutation.status == status)
    if khasra_no:
        stmt = stmt.where(Mutation.khasra_no == khasra_no)
    rows = (await session.execute(stmt)).scalars().all()
    return [MutationOut.model_validate(r) for r in rows]


@router.get("/{mutation_id}", response_model=MutationOut)
async def get_mutation(
    mutation_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    row = await session.get(Mutation, mutation_id)
    if row is None:
        raise HTTPException(404, "mutation not found")
    return MutationOut.model_validate(row)


@router.post("/{mutation_id}/evaluate", summary="Re-run the rules on a mutation")
async def evaluate_mutation(
    mutation_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.QANOONGO)),
):
    """Re-evaluate with current data.

    Useful after an officer supplies the missing evidence (a ground visit, a
    corrected CNIC). The previous evaluation is never deleted — the chain keeps
    both, which is how we can show that a BLOCK was properly resolved rather
    than quietly reversed.
    """
    from ...integrations.efard import EfardMutation
    from ...integrations.nadra import build_nadra_port
    from ..routers.integrations import get_efard_port

    row = await session.get(Mutation, mutation_id)
    if row is None:
        raise HTTPException(404, "mutation not found")

    efard = get_efard_port(settings)
    nadra = build_nadra_port(settings)

    # Prefer e-Fard's own copy: it is the system of record, and it still holds
    # the CNICs we deliberately do not mirror. Fall back to our mirror when the
    # portal cannot be reached, so an officer can still re-run the rules.
    efard_mutation = await efard.fetch_mutation(mutation_id)
    if efard_mutation is None:
        payload = row.payload or {}
        efard_mutation = EfardMutation(
            mutation_id=row.id, district_code=row.district_code,
            tehsil_code=row.tehsil_code, mouza_code=row.mouza_code,
            mouza_name=row.mouza_name or "", khewat_no=row.khewat_no,
            khasra_no=row.khasra_no, area_kanal=row.area_kanal,
            transaction_type=row.transaction_type,
            declared_price_pkr=row.declared_price_pkr, patwari_id=row.patwari_id,
            proposed_at=row.proposed_at,
            sellers=payload.get("sellers", []), buyers=payload.get("buyers", []),
            raw=payload)
    evaluation, clearance, _ = await mut_svc.run_clearance(
        session, efard_mutation, settings=settings, nadra=nadra, efard_port=efard)
    await session.commit()
    return {
        "mutation_id": mutation_id,
        "decision": clearance.decision,
        "severity": clearance.severity,
        "score": clearance.score,
        "reasons": clearance.reasons,
        "ledger_seq": clearance.ledger_seq,
        "ledger_hash": clearance.ledger_hash,
        "findings": [f.to_dict() for f in evaluation.findings],
    }


@router.get("/khasra/{district}/{mouza}/{khasra}/shares", response_model=ShareOut,
            summary="Current ownership shares on a khasra")
async def khasra_shares(
    district: str, mouza: str, khasra: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.PATWARI)),
):
    snap = await ownership.snapshot(session, district, mouza, khasra)
    return ShareOut(khasra_key=snap.khasra_key, total_share=str(snap.total_share),
                    oversubscribed=snap.oversubscribed, owners=snap.owners)


@router.get("/khasra/{district}/{mouza}/{khasra}/history",
            summary="Full append-only history for one khasra")
async def khasra_history(
    district: str, mouza: str, khasra: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.PATWARI)),
):
    from ...services import ledger
    key = ownership.khasra_key(district, mouza, khasra)
    entries = await ledger.load_entries(session, chain_key=settings.chain_key,
                                        limit=5000)
    related = [e for e in entries
               if (e.payload or {}).get("khasra_no") == khasra
               and (e.payload or {}).get("mouza") == mouza]
    return {
        "khasra_key": key,
        "events": [{
            "seq": e.seq, "event_type": e.event_type, "actor_id": e.actor_id,
            "occurred_at": e.occurred_at, "entry_hash": e.entry_hash,
            "payload": e.payload,
        } for e in related],
        "shares": (await ownership.snapshot(session, district, mouza, khasra)).to_dict(),
    }


@router.get("/audit/oversubscribed",
            summary="Every khasra whose recorded shares exceed 100%")
async def oversubscribed(
    district_code: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.TEHSILDAR)),
):
    """The single most useful query to run on day one of a pilot.

    These parcels are *already* broken — usually by decades of unrecorded
    inheritance. They are also where the next fraud will land, because a khasra
    whose shares do not add up is one nobody can confidently defend.
    """
    return await ownership.oversubscribed_khasras(session, district=district_code)
