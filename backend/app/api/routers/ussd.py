"""USSD (*444#) gateway — the smartphone-free channel."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...db.session import get_session
from ...services import ussd as ussd_svc
from ..deps import Principal, Role, require
from ..schemas import UssdIn, UssdOut

router = APIRouter(prefix="/ussd", tags=["USSD *444#"])


@router.post("/gateway", response_model=UssdOut,
             summary="Standard telco USSD callback (Africa's Talking / Jazz / Telenor shape)")
async def gateway(
    body: UssdIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.SERVICE)),
):
    """Handle one USSD keypress.

    Read-only status checks and fraud reports only. Nothing that transfers land
    is ever exposed here: USSD has no document, no display and no reliable
    identity, so it is an alert channel, not a transaction channel.
    """
    resp = await ussd_svc.handle(session, session_id=body.sessionId,
                                 msisdn=body.msisdn or "unknown", text=body.text,
                                 settings=settings)
    await session.commit()
    return UssdOut(message=resp.text, continueSession=resp.continue_session)


@router.get("/preview", summary="Walk the menu tree without a telco (for testing)")
async def preview(
    path: str = "",
    msisdn: str = "03001234567",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.PATWARI)),
):
    """Simulate a handset: ``?path=1*441*MZ-PSH-001``.

    Lets a district officer rehearse the exact screens a farmer will see before
    the short code goes live, in all three languages.
    """
    resp = await ussd_svc.handle(session, session_id=f"preview-{abs(hash(path)) % 10**8}",
                                 msisdn=msisdn, text=path, settings=settings)
    await session.rollback()
    return {"input": path, "message": resp.text,
            "continue_session": resp.continue_session, "state": resp.state.value,
            "language": resp.lang}
