"""Anti-spoof tests: every way we know of to fake a site visit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.ground_trust import (
    GroundSubmission,
    TrustBand,
    assess,
    band_for,
    haversine_m,
    implied_speed_kmh,
)

NOW = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc)


def good(**kw) -> GroundSubmission:
    base = dict(
        latitude=34.045, longitude=71.625, accuracy_m=6.0,
        captured_at=NOW, provider="gps", is_mock=False, satellites=9,
        device_id="dev-1", device_registered=True, app_attested=True,
        photo_sha256="a" * 64, photo_source="camera",
        photo_exif_datetime=NOW, witness_cnics=("h1", "h2"),
        witness_nadra_ok=(True, True), received_at=NOW + timedelta(minutes=10),
        inside_mouza=True, distance_to_boundary_m=0.0)
    return GroundSubmission(**{**base, **kw})


def failed(sub) -> set[str]:
    return {s.code for s in assess(sub).failed}


def test_a_honest_visit_scores_high():
    a = assess(good())
    assert a.band is TrustBand.HIGH
    assert a.score >= 85
    assert not a.failed


def test_gallery_photo_is_rejected_outright():
    a = assess(good(photo_source="gallery"))
    assert "G6_PHOTO_SOURCE" in failed(good(photo_source="gallery"))
    assert a.band is TrustBand.REJECTED


def test_mock_location_provider_is_rejected():
    a = assess(good(is_mock=True))
    assert "G2_MOCK" in failed(good(is_mock=True))
    assert a.score == 0
    assert a.band is TrustBand.REJECTED


def test_cell_tower_fix_is_penalised():
    """A 'network' fix can be kilometres out in rural KP."""
    assert "G2_MOCK" in failed(good(provider="network"))


def test_coarse_accuracy_is_penalised():
    assert "G1_ACCURACY" in failed(good(accuracy_m=800.0))


def test_implausibly_perfect_accuracy_is_penalised():
    assert "G1_ACCURACY" in failed(good(accuracy_m=0.05))


def test_teleporting_device_is_detected():
    sub = good(previous_fix=(31.5, 74.3, NOW - timedelta(minutes=2)))
    assert "G4_TELEPORT" in failed(sub)


def test_normal_travel_is_not_flagged():
    # ~600 m in 10 minutes = 3.6 km/h, i.e. walking.
    sub = good(latitude=34.050, longitude=71.625,
               previous_fix=(34.045, 71.625, NOW - timedelta(minutes=10)))
    assert "G4_TELEPORT" not in failed(sub)


def test_fix_outside_boundary_is_penalised():
    assert "G5_GEOFENCE" in failed(
        good(inside_mouza=False, distance_to_boundary_m=9000.0))


def test_small_boundary_error_is_tolerated():
    """Cadastral polygons are imprecise; 150 m must not fail."""
    assert "G5_GEOFENCE" not in failed(
        good(inside_mouza=False, distance_to_boundary_m=150.0))


def test_undigitised_mouza_is_unknown_not_inside():
    sub = good(inside_mouza=None, distance_to_boundary_m=None)
    assert "G5_GEOFENCE" in failed(sub)


def test_reused_photo_is_rejected():
    assert "G8_PHOTO_REUSE" in failed(good(photo_sha_seen_before=True))


def test_near_duplicate_photos_are_penalised():
    assert "G8_PHOTO_REUSE" in failed(good(phash_neighbours=4))


def test_stale_photo_exif_is_penalised():
    assert "G7_PHOTO_TIME" in failed(
        good(photo_exif_datetime=NOW - timedelta(hours=6)))


def test_future_timestamp_is_penalised():
    assert "G9_CLOCK" in failed(good(captured_at=NOW + timedelta(hours=5)))


def test_unregistered_device_is_penalised():
    assert "G11_DEVICE" in failed(good(device_registered=False))


def test_missing_attestation_is_penalised():
    assert "G11_DEVICE" in failed(good(app_attested=False))


def test_missing_witnesses_are_penalised():
    assert "G12_WITNESSES" in failed(good(witness_cnics=("h1",), witness_nadra_ok=(True,)))


def test_short_offline_window_is_accepted():
    """Offline work is normal in KP. A few hours must not fail the visit."""
    sub = good(captured_at=NOW - timedelta(hours=8), received_at=NOW)
    assert "G10_OFFLINE" not in failed(sub)


def test_very_long_offline_window_is_penalised():
    sub = good(captured_at=NOW - timedelta(hours=150), received_at=NOW)
    assert "G10_OFFLINE" in failed(sub)


def test_score_never_goes_below_zero():
    worst = GroundSubmission(latitude=0, longitude=0, is_mock=True,
                             photo_source="gallery", received_at=NOW)
    assert assess(worst).score == 0
    assert assess(worst).band is TrustBand.REJECTED


def test_haversine_and_speed_maths():
    # Peshawar to Lahore is roughly 380 km.
    d = haversine_m((34.015, 71.525), (31.549, 74.344))
    assert 370_000 < d < 400_000
    # Covering it in 30 minutes implies 760 km/h.
    v = implied_speed_kmh((34.015, 71.525), (31.549, 74.344),
                          NOW, NOW + timedelta(minutes=30))
    assert 700 < v < 820


def test_band_thresholds():
    assert band_for(95) is TrustBand.HIGH
    assert band_for(60) is TrustBand.MEDIUM
    assert band_for(30) is TrustBand.LOW
    assert band_for(0) is TrustBand.REJECTED
