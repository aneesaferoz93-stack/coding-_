"""
ground.py — ingest and score a Patwari site visit.

This is where the PWA's claims get tested. The client is untrusted: everything
it sends is a *claim*, and the point of this module is to attach a defensible
confidence score to that claim before the rule engine relies on it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core.ground_trust import (
    DEFAULT_TRUST_CONFIG,
    GroundSubmission,
    TrustAssessment,
    TrustBand,
    assess,
)
from ..core.hashchain import LedgerEvent
from ..db.geo import locate_in_mouza
from ..db.models import (
    FieldDevice,
    GroundVerification,
    OfficerRole,
    VerificationStatus,
)
from ..integrations.nadra import cnic_checksum_ok, cnic_hash, normalise_cnic
from . import ledger

log = logging.getLogger(__name__)


def attestation_body(*, nonce: str, latitude: float, longitude: float,
                     captured_at: datetime | None, photo_sha256: str | None) -> str:
    """The exact string the app HMACs with its device secret.

    Deliberately minimal and locale-independent so that the Python service, the
    TypeScript PWA and the tests all produce byte-identical input: coordinates
    at 7 decimal places, a UTC timestamp truncated to whole seconds, and the
    photo digest. Anything locale- or serialisation-dependent (a "Z" suffix, a
    trailing +00:00, a float rendering) would make valid devices fail
    attestation for no reason — which is how field apps get abandoned.
    """
    stamp = ""
    if captured_at is not None:
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        stamp = captured_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{nonce}:{float(latitude):.7f}:{float(longitude):.7f}:{stamp}:{photo_sha256 or ''}"


def hamming(a: str, b: str) -> int:
    # strict=False: the two hashes may legitimately differ in length if one
    # came from an older client, and truncating to the shorter one is the
    # conservative comparison.
    return sum(1 for x, y in zip(a, b, strict=False) if x != y)


async def _photo_reuse(session: AsyncSession, sha: str | None,
                       phash: str | None) -> tuple[bool, int]:
    """Exact-duplicate and near-duplicate photo detection."""
    if not sha:
        return False, 0
    exists = (await session.execute(
        select(GroundVerification.id).where(GroundVerification.photo_sha256 == sha).limit(1)
    )).scalar_one_or_none()
    if exists:
        return True, 0

    neighbours = 0
    if phash:
        rows = (await session.execute(
            select(GroundVerification.photo_phash)
            .where(GroundVerification.photo_phash.is_not(None))
            .limit(2000)
        )).scalars().all()
        neighbours = sum(1 for p in rows if p and hamming(p, phash) <= 6)
    return False, neighbours


async def _previous_fix(session: AsyncSession, device_id: str | None,
                        before: datetime | None):
    if not device_id or not before:
        return None
    row = (await session.execute(
        select(GroundVerification.latitude, GroundVerification.longitude,
               GroundVerification.captured_at)
        .where(GroundVerification.device_id == device_id)
        .where(GroundVerification.captured_at < before)
        .order_by(GroundVerification.captured_at.desc()).limit(1)
    )).first()
    if not row or row[0] is None or row[1] is None or row[2] is None:
        return None
    when = row[2]
    # SQLite hands back naive datetimes; Postgres returns aware ones. The
    # teleport check subtracts these, and Python refuses to mix the two.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (row[0], row[1], when)


async def submit(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    settings: Settings,
    nadra=None,
) -> tuple[GroundVerification, TrustAssessment]:
    """Score and store one site visit."""
    device_id = payload.get("device_id")
    officer_id = payload.get("officer_id")

    def dt(v: Any):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    device = None
    if device_id:
        device = await session.get(FieldDevice, device_id)
    device_registered = bool(device and not device.revoked
                             and (not officer_id or device.officer_id == officer_id))

    # App attestation: HMAC over nonce + canonical payload, using the secret
    # issued when the handset was enrolled. Proves the request came from our app.
    attested = False
    if device and device.attestation_secret and payload.get("nonce") and payload.get("hmac"):
        import hmac as _hmac

        body = attestation_body(
            nonce=str(payload.get("nonce")),
            latitude=float(payload.get("latitude") or 0),
            longitude=float(payload.get("longitude") or 0),
            captured_at=dt(payload.get("captured_at")),
            photo_sha256=payload.get("photo_sha256"))
        expected = _hmac.new(device.attestation_secret.encode("utf-8"),
                             body.encode("utf-8"), hashlib.sha256).hexdigest()
        attested = _hmac.compare_digest(expected, str(payload.get("hmac")))

    captured_at = dt(payload.get("captured_at")) or datetime.now(timezone.utc)
    received_at = datetime.now(timezone.utc)

    lat = float(payload["latitude"])
    lon = float(payload["longitude"])

    geo = await locate_in_mouza(session, str(payload.get("mouza_code") or ""), lat, lon)

    sha = payload.get("photo_sha256")
    phash = payload.get("photo_phash")
    seen_before, neighbours = await _photo_reuse(session, sha, phash)

    prev = await _previous_fix(session, device_id, captured_at)

    # Witness CNICs are verified against NADRA, then discarded — only hashes stay.
    witness_hashes: list[str] = []
    witness_ok: list[bool] = []
    for cnic in (payload.get("witness_cnics") or [])[:6]:
        if not cnic:
            continue
        ok = cnic_checksum_ok(str(cnic))
        if ok and nadra is not None:
            try:
                res = await nadra.verify_party(str(cnic))
                ok = res.outcome.value == "LIVE"
            except Exception as exc:
                log.warning("witness NADRA check failed: %s", exc)
                ok = False
        witness_hashes.append(cnic_hash(normalise_cnic(str(cnic)),
                                        settings.ledger_signing_key))
        witness_ok.append(ok)

    sub = GroundSubmission(
        latitude=lat, longitude=lon,
        accuracy_m=(float(payload["accuracy_m"]) if payload.get("accuracy_m") is not None else None),
        altitude_m=(float(payload["altitude_m"]) if payload.get("altitude_m") is not None else None),
        captured_at=captured_at,
        monotonic_ms=(int(payload["monotonic_ms"]) if payload.get("monotonic_ms") is not None else None),
        provider=payload.get("provider"),
        is_mock=bool(payload.get("is_mock")),
        satellites=(int(payload["satellites"]) if payload.get("satellites") is not None else None),
        device_id=device_id,
        officer_id=officer_id,
        device_registered=device_registered,
        app_attested=attested,
        photo_sha256=sha,
        photo_phash=phash,
        photo_bytes=(int(payload["photo_bytes"]) if payload.get("photo_bytes") else None),
        photo_exif_datetime=dt(payload.get("photo_exif_datetime")),
        photo_source=str(payload.get("photo_source") or "unknown"),
        witness_cnics=tuple(witness_hashes),
        witness_nadra_ok=tuple(witness_ok),
        received_at=received_at,
        previous_fix=prev,
        photo_sha_seen_before=seen_before,
        phash_neighbours=neighbours,
        inside_mouza=geo.inside,
        distance_to_boundary_m=geo.distance_m,
    )

    assessment = assess(sub, DEFAULT_TRUST_CONFIG)

    accepted = (assessment.band in (TrustBand.HIGH, TrustBand.MEDIUM)
                and assessment.score >= settings.ground_trust_accept_min)

    vid = payload.get("client_submission_id") or f"GV-{int(received_at.timestamp())}-" \
        f"{hashlib.sha256(f'{device_id}{lat}{lon}{captured_at}'.encode()).hexdigest()[:10]}"

    row = GroundVerification(
        id=vid,
        mutation_id=payload.get("mutation_id"),
        khasra_key=ownership_key(payload),
        district_code=str(payload.get("district_code") or "").upper(),
        mouza_code=str(payload.get("mouza_code") or ""),
        khasra_no=(str(payload["khasra_no"]) if payload.get("khasra_no") else None),
        officer_id=officer_id,
        device_id=device_id,
        latitude=lat, longitude=lon,
        accuracy_m=sub.accuracy_m, altitude_m=sub.altitude_m,
        provider=sub.provider, is_mock=sub.is_mock, satellites=sub.satellites,
        geom_wkt=f"POINT({lon:.7f} {lat:.7f})",
        inside_mouza=geo.inside,
        distance_to_boundary_m=geo.distance_m,
        photo_sha256=sha, photo_phash=phash, photo_bytes=sub.photo_bytes,
        photo_exif_datetime=sub.photo_exif_datetime, photo_source=sub.photo_source,
        witness_cnic_hashes=list(witness_hashes),
        witness_nadra_ok=witness_ok,
        trust_score=assessment.score,
        trust_band=assessment.band.value,
        trust_signals=[dataclasses.asdict(s) for s in assessment.signals],
        trust_config_version=assessment.config_version,
        status=(VerificationStatus.ACCEPTED if accepted
                else VerificationStatus.REJECTED),
        captured_at=captured_at,
        received_at=received_at,
        offline_hours=round((received_at - captured_at).total_seconds() / 3600, 2),
        app_version=payload.get("app_version"),
        client_submission_id=payload.get("client_submission_id"),
        notes=payload.get("notes"),
    )
    session.add(row)
    await session.flush()

    await ledger.append(
        session,
        event_type=(LedgerEvent.GROUND_VERIFICATION if accepted
                    else LedgerEvent.GROUND_REJECTED),
        entity_type="ground_verification",
        entity_id=vid,
        actor_id=officer_id or "unknown",
        actor_role=OfficerRole.PATWARI.value,
        district_code=row.district_code,
        payload={
            "mutation_id": row.mutation_id,
            "mouza": row.mouza_code,
            "khasra_no": row.khasra_no,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "accuracy_m": sub.accuracy_m,
            "inside_mouza": geo.inside,
            "geo_engine": geo.engine,
            "distance_to_boundary_m": geo.distance_m,
            "photo_sha256": sha,
            "photo_source": sub.photo_source,
            "witnesses": len(witness_hashes),
            "witness_verified": sum(1 for x in witness_ok if x),
            "trust_score": assessment.score,
            "trust_band": assessment.band.value,
            "failed_signals": [s.code for s in assessment.failed],
            "offline_hours": row.offline_hours,
            "device_registered": device_registered,
            "app_attested": attested,
        },
        settings=settings,
        occurred_at=captured_at,
    )
    return row, assessment


def ownership_key(payload: dict[str, Any]) -> str | None:
    d = payload.get("district_code")
    m = payload.get("mouza_code")
    k = payload.get("khasra_no")
    if d and m and k:
        return f"{str(d).upper()}/{m}/{k}"
    return None
