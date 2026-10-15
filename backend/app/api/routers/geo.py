"""Geo endpoints for the Patwari app: cadastral boundaries and work assignments."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import FraudFlag, GroundVerification, Mouza, Mutation
from ...db.session import get_session
from ..deps import Principal, Role, require

router = APIRouter(prefix="/geo", tags=["geo"])


@router.get("/mouza/{mouza_code}", summary="Boundary polygon + centroid for a mouza")
async def mouza_boundary(
    mouza_code: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.PATWARI)),
):
    """Returns the digitised boundary as WKT so the app can draw it offline.

    If ``digitised`` is false the app must tell the Patwari that geofencing is
    unavailable here — not silently pretend the fix was inside the boundary.
    """
    mouza = await session.get(Mouza, mouza_code)
    if mouza is None:
        raise HTTPException(404, "mouza not found")
    return {
        "code": mouza.code,
        "name_en": mouza.name_en,
        "name_ur": mouza.name_ur,
        "name_ps": mouza.name_ps,
        "district_code": mouza.district_code,
        "tehsil_code": mouza.tehsil_code,
        "digitised": mouza.digitised,
        "boundary_wkt": mouza.boundary_wkt,
        "centroid": {"lat": mouza.centroid_lat, "lon": mouza.centroid_lon},
        "latha_scanned_pct": (float(mouza.latha_scanned_pct)
                              if mouza.latha_scanned_pct is not None else None),
        "total_khasras": mouza.total_khasras,
    }


@router.get("/district/{district_code}/mouzas", summary="Mouzas in a district")
async def district_mouzas(
    district_code: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.PATWARI)),
):
    rows = (await session.execute(
        select(Mouza).where(Mouza.district_code == district_code.upper())
        .order_by(Mouza.name_en))).scalars().all()
    return [{
        "code": m.code, "name_en": m.name_en, "name_ur": m.name_ur,
        "name_ps": m.name_ps, "digitised": m.digitised,
        "centroid": {"lat": m.centroid_lat, "lon": m.centroid_lon},
    } for m in rows]


@router.get("/assignments", summary="The Patwari's pending site-visit work list")
async def assignments(
    patwari_id: str | None = None,
    district_code: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.PATWARI)),
):
    """What needs a site visit today.

    Deliberately small payloads: this list is cached offline on the handset, and
    a Patwari in Matta may be loading it on 2G.
    """
    stmt = (select(Mutation)
            .where(Mutation.status.in_(["DRAFT", "HELD", "BLOCKED"]))
            .order_by(Mutation.proposed_at.desc()).limit(limit))
    if patwari_id:
        stmt = stmt.where(Mutation.patwari_id == patwari_id)
    if district_code:
        stmt = stmt.where(Mutation.district_code == district_code.upper())
    rows = (await session.execute(stmt)).scalars().all()

    out: list[dict[str, Any]] = []
    for m in rows:
        flags = (await session.execute(
            select(FraudFlag.rule_id, FraudFlag.severity)
            .where(FraudFlag.mutation_id == m.id)
            .where(FraudFlag.severity.in_(["RED", "AMBER"]))
        )).all()
        done = (await session.execute(
            select(GroundVerification.id)
            .where(GroundVerification.mutation_id == m.id)
            .where(GroundVerification.status == "ACCEPTED").limit(1)
        )).scalar_one_or_none()
        out.append({
            "mutation_id": m.id,
            "district_code": m.district_code,
            "tehsil_code": m.tehsil_code,
            "mouza_code": m.mouza_code,
            "mouza_name": m.mouza_name,
            "khewat_no": m.khewat_no,
            "khasra_no": m.khasra_no,
            "area_kanal": str(m.area_kanal),
            "transaction_type": m.transaction_type,
            "status": m.status.value if hasattr(m.status, "value") else m.status,
            "proposed_at": m.proposed_at,
            "flags": [{"rule_id": r, "severity": s} for r, s in flags],
            "ground_verified": bool(done),
        })
    return out
