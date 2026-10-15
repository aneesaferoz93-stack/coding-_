"""
ground_trust.py — how hard is it to fake a site visit?

The Patwari app's whole value is the claim "a human stood on this parcel at
this time and took this photograph". Every one of those three claims can be
forged, so each gets its own check, and the checks are scored rather than
passed/failed — because in rural KP a weak GPS fix or a 30-hour offline window
is usually *terrain*, not fraud. A binary rule that rejects honest field staff
in Kohistan will be switched off within a month, and then we have no rule at
all.

Checks implemented
------------------
G1  GPS accuracy            — coarse fixes are weak evidence of presence
G2  Mock provider           — Android mock-location / fused provider flags
G3  Satellite support       — a fix with no satellites is a software artefact
G4  Teleport                — implied travel speed between successive fixes
G5  Geofence                — is the fix inside the mouza polygon?
G6  Photo provenance        — camera capture required; gallery uploads rejected
G7  Photo/GPS correlation   — EXIF time must sit next to the GPS fix time
G8  Photo reuse             — identical (or near-identical) photo hash elsewhere
G9  Clock sanity            — device clock vs server clock skew, and rollback
G10 Offline window          — long offline periods weaken the evidence
G11 Device binding          — is this a registered device for this officer?
G12 Witness completeness    — two neighbours' CNICs, NADRA-checked

The output is a 0-100 trust score plus the explicit list of failed signals, so
a Tehsildar can see *why* a visit was discounted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

TRUST_VERSION = "1.1.0"


class TrustBand(str, Enum):
    HIGH = "HIGH"        # 80-100  usable as positive evidence
    MEDIUM = "MEDIUM"    # 50-79   usable, but corroborate
    LOW = "LOW"          # 25-49   do not rely on alone
    REJECTED = "REJECTED"  # <25   evidence discarded


@dataclass(frozen=True)
class TrustConfig:
    version: str = TRUST_VERSION
    max_accuracy_m: float = 30.0        # G1
    min_accuracy_m: float = 2.0         # implausibly perfect -> indoor/spoofed
    min_satellites: int = 4             # G3
    max_implied_speed_kmh: float = 250.0  # G4 (helicopter ceiling)
    geofence_m: float = 250.0           # G5
    max_photo_gap_s: int = 300          # G7: photo within 5 min of the fix
    max_clock_skew_s: int = 900         # G9
    offline_warn_h: float = 24.0        # G10
    offline_fail_h: float = 96.0        # G10
    required_witnesses: int = 2         # G12


DEFAULT_TRUST_CONFIG = TrustConfig()


@dataclass(slots=True)
class Signal:
    code: str
    passed: bool
    detail: str
    penalty: int = 0     # points deducted when failed
    value: object = None


@dataclass(slots=True)
class TrustAssessment:
    score: int
    band: TrustBand
    signals: list[Signal] = field(default_factory=list)
    config_version: str = TRUST_VERSION
    assessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def failed(self) -> list[Signal]:
        return [s for s in self.signals if not s.passed]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "band": self.band.value,
            "config_version": self.config_version,
            "assessed_at": self.assessed_at,
            "signals": [
                {"code": s.code, "passed": s.passed, "detail": s.detail,
                 "penalty": s.penalty, "value": s.value}
                for s in self.signals
            ],
        }


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def implied_speed_kmh(
    a: tuple[float, float], b: tuple[float, float],
    t_a: datetime, t_b: datetime,
) -> float | None:
    """Average speed required to get from fix A to fix B."""
    seconds = abs((t_b - t_a).total_seconds())
    if seconds < 1:
        return None
    return haversine_m(a, b) / seconds * 3.6


@dataclass(slots=True)
class GroundSubmission:
    """Raw payload from the Patwari PWA (one site visit)."""

    latitude: float
    longitude: float
    accuracy_m: float | None = None
    altitude_m: float | None = None
    captured_at: datetime | None = None            # device wall clock
    monotonic_ms: int | None = None                # elapsedRealtime since boot
    provider: str | None = None                    # "gps" | "fused" | "network"
    is_mock: bool | None = None                    # Android isMockLocation
    satellites: int | None = None
    device_id: str | None = None
    officer_id: str | None = None
    device_registered: bool = False
    app_attested: bool = False                     # HMAC over the payload+nonce

    photo_sha256: str | None = None
    photo_phash: str | None = None
    photo_bytes: int | None = None
    photo_exif_datetime: datetime | None = None
    photo_source: str = "camera"                   # "camera" | "gallery" | "unknown"

    witness_cnics: Sequence[str] = ()
    witness_nadra_ok: Sequence[bool] = ()

    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    previous_fix: tuple[float, float, datetime] | None = None   # for G4
    photo_sha_seen_before: bool = False
    phash_neighbours: int = 0                      # near-duplicate photos in DB
    inside_mouza: bool | None = None
    distance_to_boundary_m: float | None = None


def assess(
    sub: GroundSubmission,
    cfg: TrustConfig = DEFAULT_TRUST_CONFIG,
) -> TrustAssessment:
    """Score one ground submission. Pure function — no DB, no clock, no IO."""
    score = 100
    signals: list[Signal] = []

    def add(code: str, passed: bool, detail: str, penalty: int, value=None) -> None:
        nonlocal score
        if not passed:
            score -= penalty
        signals.append(Signal(code=code, passed=passed, detail=detail,
                              penalty=penalty if not passed else 0, value=value))

    # ---- G1 accuracy -------------------------------------------------------
    acc = sub.accuracy_m
    if acc is None:
        add("G1_ACCURACY", False, "No GPS accuracy reported.", 20)
    elif acc > cfg.max_accuracy_m:
        add("G1_ACCURACY", False,
            f"GPS accuracy ±{acc:.0f} m is coarser than the {cfg.max_accuracy_m:.0f} m limit; "
            "the fix does not establish presence on a specific parcel.",
            25, acc)
    elif acc < cfg.min_accuracy_m:
        add("G1_ACCURACY", False,
            f"GPS accuracy ±{acc:.2f} m is implausibly precise for a consumer handset "
            "outdoors, which is characteristic of an injected fix.",
            15, acc)
    else:
        add("G1_ACCURACY", True, f"GPS accuracy ±{acc:.0f} m acceptable.", 0, acc)

    # ---- G2 mock provider --------------------------------------------------
    if sub.is_mock:
        add("G2_MOCK", False,
            "Device reports a mock/simulated location provider. This is a spoof.", 100)
    elif (sub.provider or "").lower() in {"network", "passive", "cell"}:
        add("G2_MOCK", False,
            f"Fix came from the '{sub.provider}' provider (cell/Wi-Fi triangulation), "
            "which can be displaced by kilometres in rural KP.", 40, sub.provider)
    else:
        add("G2_MOCK", True,
            f"Fix from '{sub.provider or 'unknown'}' provider; mock flag not set.", 0,
            sub.provider)

    # ---- G3 satellites -----------------------------------------------------
    if sub.satellites is not None and sub.satellites < cfg.min_satellites:
        add("G3_SATELLITES", False,
            f"Only {sub.satellites} satellites supported the fix "
            f"(minimum {cfg.min_satellites}).", 15, sub.satellites)
    else:
        add("G3_SATELLITES", True, "Satellite support adequate.", 0, sub.satellites)

    # ---- G4 teleport -------------------------------------------------------
    if sub.previous_fix and sub.captured_at:
        plat, plon, ptime = sub.previous_fix
        speed = implied_speed_kmh((plat, plon), (sub.latitude, sub.longitude),
                                  ptime, sub.captured_at)
        if speed is not None and speed > cfg.max_implied_speed_kmh:
            add("G4_TELEPORT", False,
                f"Implied travel speed {speed:,.0f} km/h between consecutive fixes "
                f"(limit {cfg.max_implied_speed_kmh:.0f} km/h). The device moved "
                "faster than any vehicle — the location was set, not measured.",
                60, round(speed, 1))
        else:
            add("G4_TELEPORT", True,
                "Movement between fixes is physically plausible.", 0,
                round(speed, 1) if speed is not None else None)
    else:
        add("G4_TELEPORT", True, "No prior fix to compare against.", 0)

    # ---- G5 geofence -------------------------------------------------------
    if sub.inside_mouza is None:
        add("G5_GEOFENCE", False,
            "Mouza boundary could not be resolved (missing or undigitised cadastral "
            "polygon), so the fix cannot be placed on the parcel.", 30)
    elif sub.inside_mouza:
        add("G5_GEOFENCE", True, "Fix lies inside the mouza boundary.", 0,
            sub.distance_to_boundary_m)
    else:
        dist = sub.distance_to_boundary_m
        if dist is not None and dist <= cfg.geofence_m:
            add("G5_GEOFENCE", True,
                f"Fix is {dist:.0f} m outside the boundary — within the "
                f"{cfg.geofence_m:.0f} m tolerance for boundary-digitisation error.",
                0, dist)
        else:
            add("G5_GEOFENCE", False,
                "Fix is outside the mouza boundary"
                + (f" by ~{dist:.0f} m" if dist is not None else "")
                + ". The officer was not on this land.", 50, dist)

    # ---- G6 photo provenance ----------------------------------------------
    if sub.photo_source != "camera":
        add("G6_PHOTO_SOURCE", False,
            f"Photo source is '{sub.photo_source}'. Only a live camera capture is "
            "accepted; a gallery upload proves nothing about presence.", 100,
            sub.photo_source)
    elif not sub.photo_sha256:
        add("G6_PHOTO_SOURCE", False, "No photo supplied.", 100)
    else:
        add("G6_PHOTO_SOURCE", True, "Live camera capture.", 0)

    # ---- G7 photo/GPS correlation -----------------------------------------
    if sub.photo_exif_datetime and sub.captured_at:
        gap = abs((sub.photo_exif_datetime - sub.captured_at).total_seconds())
        if gap > cfg.max_photo_gap_s:
            add("G7_PHOTO_TIME", False,
                f"Photo EXIF time and GPS fix time differ by {gap / 60:.0f} min "
                f"(limit {cfg.max_photo_gap_s / 60:.0f} min). The photo was probably "
                "taken somewhere else, or at a different time.", 35, round(gap))
        else:
            add("G7_PHOTO_TIME", True,
                f"Photo and fix within {gap:.0f} s of each other.", 0, round(gap))
    else:
        add("G7_PHOTO_TIME", False,
            "Photo carries no EXIF timestamp (stripped or unavailable).", 20)

    # ---- G8 photo reuse ----------------------------------------------------
    if sub.photo_sha_seen_before:
        add("G8_PHOTO_REUSE", False,
            "This exact photograph has already been submitted for a different "
            "verification. Site photos are being reused.", 80)
    elif sub.phash_neighbours >= 3:
        add("G8_PHOTO_REUSE", False,
            f"{sub.phash_neighbours} near-identical photos already exist in the "
            "corpus (perceptual-hash match).", 40, sub.phash_neighbours)
    else:
        add("G8_PHOTO_REUSE", True, "Photo is not a known duplicate.", 0,
            sub.phash_neighbours)

    # ---- G9 clock ----------------------------------------------------------
    if sub.captured_at:
        skew = abs((sub.received_at - sub.captured_at).total_seconds())
        if sub.captured_at > sub.received_at + timedelta(seconds=cfg.max_clock_skew_s):
            add("G9_CLOCK", False,
                f"Evidence is timestamped {skew / 60:.0f} min in the future.", 70,
                round(skew))
        elif skew > 86400:
            add("G9_CLOCK", False,
                f"Evidence is {skew / 3600:.0f} h older than the sync time.", 30,
                round(skew / 3600, 1))
        else:
            add("G9_CLOCK", True, f"Clock skew {skew / 60:.0f} min within tolerance.",
                0, round(skew))
    else:
        add("G9_CLOCK", False, "No capture timestamp supplied.", 50)

    # ---- G10 offline window -------------------------------------------------
    if sub.captured_at:
        offline_h = (sub.received_at - sub.captured_at).total_seconds() / 3600
        if offline_h > cfg.offline_fail_h:
            add("G10_OFFLINE", False,
                f"Synced {offline_h:.0f} h after capture "
                f"(limit {cfg.offline_fail_h:.0f} h). Beyond this window we cannot "
                "distinguish a connectivity gap from staged evidence.", 45,
                round(offline_h, 1))
        elif offline_h > cfg.offline_warn_h:
            add("G10_OFFLINE", False,
                f"Synced {offline_h:.0f} h after capture. Offline work is expected in "
                "remote KP, but the evidence is weaker; corroborate with EXIF.", 15,
                round(offline_h, 1))
        else:
            add("G10_OFFLINE", True,
                f"Synced {offline_h:.1f} h after capture.", 0, round(offline_h, 1))
    else:
        add("G10_OFFLINE", False, "Cannot compute an offline window without a capture time.", 20)

    # ---- G11 device binding --------------------------------------------------
    # Device binding is what turns "a phone said it was there" into "Officer
    # X's phone said it was there". Without it there is no attribution, so the
    # evidence is not merely weaker — it is not usable at all. These penalties
    # are set to push the score below the acceptance threshold on their own.
    if not sub.device_id:
        add("G11_DEVICE", False, "No device identifier supplied.", 60)
    elif not sub.device_registered:
        add("G11_DEVICE", False,
            f"Device {sub.device_id} is not registered to this officer. Evidence "
            "from an unregistered handset cannot be attributed to anyone.", 60,
            sub.device_id)
    elif not sub.app_attested:
        add("G11_DEVICE", False,
            "Payload failed app attestation (HMAC over nonce+payload). The request "
            "may not have originated from the official app, so it cannot be "
            "attributed to the enrolled device.", 60)
    else:
        add("G11_DEVICE", True, "Registered device, attested payload.", 0, sub.device_id)

    # ---- G12 witnesses ---------------------------------------------------------
    n = len(sub.witness_cnics)
    ok = sum(1 for x in sub.witness_nadra_ok if x)
    if n < cfg.required_witnesses:
        add("G12_WITNESSES", False,
            f"{n} witness CNIC(s) captured, {cfg.required_witnesses} required.", 20, n)
    elif ok < cfg.required_witnesses:
        add("G12_WITNESSES", False,
            f"Only {ok} of {n} witness CNICs verified against NADRA.", 15, ok)
    else:
        add("G12_WITNESSES", True, f"{ok} neighbour CNICs verified.", 0, ok)

    score = max(0, min(100, score))
    return TrustAssessment(score=score, band=band_for(score), signals=signals,
                           config_version=cfg.version)


def band_for(score: int) -> TrustBand:
    if score >= 80:
        return TrustBand.HIGH
    if score >= 50:
        return TrustBand.MEDIUM
    if score >= 25:
        return TrustBand.LOW
    return TrustBand.REJECTED


def as_ground_evidence(assessment: TrustAssessment, sub: GroundSubmission):
    """Adapt a trust assessment into the structure the rule engine consumes."""
    from .fraud_rules import GroundEvidence  # local import avoids a cycle

    def sig(code: str) -> Signal | None:
        return next((s for s in assessment.signals if s.code == code), None)

    mock = sig("G2_MOCK") or sig("G4_TELEPORT")

    return GroundEvidence(
        verified=assessment.band in (TrustBand.HIGH, TrustBand.MEDIUM),
        latitude=sub.latitude,
        longitude=sub.longitude,
        accuracy_m=sub.accuracy_m,
        captured_at=sub.captured_at,
        synced_at=sub.received_at,
        inside_mouza=sub.inside_mouza,
        distance_to_boundary_m=sub.distance_to_boundary_m,
        photo_sha256=sub.photo_sha256,
        photo_exif_datetime=sub.photo_exif_datetime,
        photo_from_camera=sub.photo_source == "camera",
        witness_cnics=tuple(sub.witness_cnics),
        witnesses_biometric=tuple(sub.witness_nadra_ok),
        device_id=sub.device_id,
        mock_location_suspected=bool(mock and not mock.passed),
        trust_score=assessment.score,
    )
