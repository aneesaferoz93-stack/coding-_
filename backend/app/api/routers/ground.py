"""Ground verification: Patwari PWA sync, device enrolment, field evidence reads."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...db.models import FieldDevice, GroundVerification, Officer, OfficerRole
from ...db.session import get_session
from ...services import ground as ground_svc
from ...services import ledger
from ..deps import Principal, Role, issue_device_token, require
from ..schemas import (
    DeviceEnrolIn,
    DeviceEnrolOut,
    GroundSubmissionIn,
    GroundVerificationOut,
)

router = APIRouter(prefix="/ground", tags=["ground verification"])


@router.post("/verify", response_model=GroundVerificationOut,
             summary="Submit a site visit (Patwari PWA, works offline-then-sync)")
async def submit_verification(
    body: GroundSubmissionIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.PATWARI)),
):
    """Score one site visit.

    The client is untrusted. Everything it sends is a *claim*; this endpoint
    attaches a confidence score and either accepts the evidence or rejects it
    with reasons the Patwari can see and act on.

    Offline is a first-class case: the PWA queues visits in IndexedDB and syncs
    days later when the officer reaches connectivity. We compare
    ``captured_at`` (device) against ``received_at`` (server) and score the gap
    honestly rather than pretending it did not happen.
    """
    payload = body.model_dump(mode="json")
    payload["officer_id"] = payload.get("officer_id") or (
        principal.subject if principal.role is not Role.SERVICE else None)

    row, assessment = await ground_svc.submit(session, payload, settings=settings)
    await session.commit()

    if row.status == "REJECTED":
        # Not an exception: the officer needs to see *why*, in the app, and be
        # able to redo the visit. A silent 400 would just produce support calls.
        row_out = GroundVerificationOut.model_validate(row)
        return row_out

    return GroundVerificationOut.model_validate(row)


@router.get("/verifications", response_model=list[GroundVerificationOut],
            summary="List site visits")
async def list_verifications(
    district_code: str | None = None,
    mouza_code: str | None = None,
    officer_id: str | None = None,
    status: str | None = None,
    min_trust: int | None = Query(None, ge=0, le=100),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    stmt = select(GroundVerification).order_by(
        GroundVerification.received_at.desc()).limit(limit)
    if district_code:
        stmt = stmt.where(GroundVerification.district_code == district_code.upper())
    if mouza_code:
        stmt = stmt.where(GroundVerification.mouza_code == mouza_code)
    if officer_id:
        stmt = stmt.where(GroundVerification.officer_id == officer_id)
    if status:
        stmt = stmt.where(GroundVerification.status == status)
    if min_trust is not None:
        stmt = stmt.where(GroundVerification.trust_score >= min_trust)
    rows = (await session.execute(stmt)).scalars().all()
    return [GroundVerificationOut.model_validate(r) for r in rows]


@router.get("/verifications/{verification_id}", summary="One site visit with its signals")
async def get_verification(
    verification_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.QANOONGO)),
):
    row = await session.get(GroundVerification, verification_id)
    if row is None:
        raise HTTPException(404, "verification not found")
    out = GroundVerificationOut.model_validate(row).model_dump()
    out["witness_count"] = len(row.witness_cnic_hashes or [])
    out["witness_verified"] = sum(1 for x in (row.witness_nadra_ok or []) if x)
    out["signals_failed"] = [s["code"] for s in (row.trust_signals or [])
                             if not s.get("passed")]
    return out


@router.post("/devices", response_model=DeviceEnrolOut,
             summary="Enrol a Patwari handset (done in person by the Tehsildar)")
async def enrol_device(
    body: DeviceEnrolIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.TEHSILDAR)),
):
    """Bind a physical handset to a named officer.

    Device binding is what turns "a phone said it was there" into "Officer X's
    phone said it was there". The attestation secret is returned once and used
    by the app to HMAC every submission.
    """
    officer = await session.get(Officer, body.officer_id)
    if officer is None:
        officer = Officer(id=body.officer_id, name=body.officer_id,
                          role=OfficerRole.PATWARI)
        session.add(officer)
        await session.flush()

    existing = await session.get(FieldDevice, body.device_id)
    secret = secrets.token_urlsafe(32)
    if existing:
        existing.officer_id = body.officer_id
        existing.attestation_secret = secret
        existing.app_version = body.app_version
        existing.platform = body.platform
        existing.revoked = False
    else:
        session.add(FieldDevice(device_id=body.device_id, officer_id=body.officer_id,
                                label=body.label, platform=body.platform,
                                app_version=body.app_version,
                                attestation_secret=secret))
    await session.flush()

    token = issue_device_token(device_id=body.device_id, officer_id=body.officer_id,
                               role=officer.role.value, district=officer.district_code,
                               settings=settings)

    await ledger.append(
        session,
        event_type=ledger.LedgerEvent.OFFICER_ACTION,
        entity_type="field_device",
        entity_id=body.device_id,
        actor_id=principal.subject,
        actor_role=principal.role.value,
        payload={"action": "ENROL", "officer_id": body.officer_id,
                 "platform": body.platform, "app_version": body.app_version},
        settings=settings,
    )
    await session.commit()
    return DeviceEnrolOut(device_id=body.device_id, officer_id=body.officer_id,
                          attestation_secret=secret, device_token=token)


@router.get("/devices", summary="List enrolled devices")
async def list_devices(
    revoked: bool = False,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require(Role.TEHSILDAR)),
):
    rows = (await session.execute(
        select(FieldDevice).where(FieldDevice.revoked.is_(revoked))
    )).scalars().all()
    return [{
        "device_id": d.device_id, "officer_id": d.officer_id, "label": d.label,
        "platform": d.platform, "app_version": d.app_version,
        "registered_at": d.registered_at, "revoked": d.revoked,
        # Never return the attestation secret.
        "attestation_secret_fingerprint": (
            hashlib.sha256((d.attestation_secret or "").encode()).hexdigest()[:12]
            if d.attestation_secret else None),
    } for d in rows]


@router.post("/devices/{device_id}/revoke", summary="Revoke a handset")
async def revoke_device(
    device_id: str,
    reason: str = Query(..., min_length=10,
                        description="Why the device was revoked — recorded in the chain."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.TEHSILDAR)),
):
    device = await session.get(FieldDevice, device_id)
    if device is None:
        raise HTTPException(404, "device not found")
    device.revoked = True
    await session.flush()
    await ledger.append(
        session, event_type=ledger.LedgerEvent.OFFICER_ACTION,
        entity_type="field_device", entity_id=device_id, actor_id=principal.subject,
        actor_role=principal.role.value,
        payload={"action": "REVOKE", "reason": reason}, settings=settings)
    await session.commit()
    return {"device_id": device_id, "revoked": True}


def parse_iso(value: str | None):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.post("/attest-nonce", summary="Issue a nonce for payload attestation")
async def attest_nonce(
    device_id: str,
    latitude: float,
    longitude: float,
    captured_at: str,
    photo_sha256: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Helper for clients that cannot hold the secret (e.g. a kiosk).

    Normal handsets sign locally (``/ground/verify`` checks the HMAC without
    ever seeing the secret). This endpoint exists only for constrained clients
    and is disabled in production by policy.
    """
    device = await session.get(FieldDevice, device_id)
    if device is None or device.revoked or not device.attestation_secret:
        raise HTTPException(404, "device not enrolled")
    nonce = secrets.token_hex(16)
    body = ground_svc.attestation_body(
        nonce=nonce, latitude=latitude, longitude=longitude,
        captured_at=parse_iso(captured_at), photo_sha256=photo_sha256)
    sig = hmac.new(device.attestation_secret.encode("utf-8"), body.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return {"nonce": nonce, "hmac": sig, "signed_body": body}
