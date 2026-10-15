"""Rule engine tests — one section per rule, plus the false-positive guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.fraud_rules import (
    DEFAULT_CONFIG,
    EvaluationResult,
    GroundEvidence,
    LathaRecord,
    MutationContext,
    NadraStatus,
    Party,
    Severity,
    TransactionType,
    evaluate,
    mask_cnic,
    name_similarity,
    normalise_name,
)

NOW = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)


def ctx(**kw) -> MutationContext:
    base = dict(
        mutation_id="M1", district_code="PESHAWAR", tehsil_code="PESHAWAR",
        mouza_code="MZ-PSH-001", mouza_name="Landi Arbab", khasra_no="441",
        area_kanal=Decimal("8"), transaction_type=TransactionType.SALE,
        declared_price_pkr=Decimal("10000000"), dc_rate_per_kanal=Decimal("1200000"),
        registered_at=NOW, patwari_id="P1",
        sellers=(Party(name="Gul Muhammad Khan", cnic="1730112345671",
                       nadra_status=NadraStatus.LIVE, biometric_verified=True,
                       age=55),),
        buyers=(Party(name="Asmatullah", cnic="1730198765432",
                      nadra_status=NadraStatus.LIVE, biometric_verified=True),),
    )
    return MutationContext(**{**base, **kw})


def find(result: EvaluationResult, rule_id: str):
    return [f for f in result.findings if f.rule_id == rule_id]


# --------------------------------------------------------------------------
# R1 — rapid re-mutation
# --------------------------------------------------------------------------

def test_r1_fires_when_same_khasra_mutated_twice_in_30_days():
    r = evaluate(ctx(prior_mutations=(NOW - timedelta(days=10),)))
    reds = [f for f in find(r, "R1") if f.severity is Severity.RED]
    assert reds
    assert "2x in 30 days" in reds[0].title


def test_r1_does_not_fire_outside_the_window():
    r = evaluate(ctx(prior_mutations=(NOW - timedelta(days=45),)))
    assert not [f for f in find(r, "R1") if f.severity is Severity.RED]


def test_r1_is_quiet_on_a_first_mutation():
    assert not find(evaluate(ctx()), "R1")


def test_r1_amber_in_the_watch_window():
    r = evaluate(ctx(prior_mutations=(NOW - timedelta(days=40),
                                      NOW - timedelta(days=50))))
    ambers = [f for f in find(r, "R1") if f.severity is Severity.AMBER]
    assert ambers


# --------------------------------------------------------------------------
# R2 — undervaluation vs DC rate
# --------------------------------------------------------------------------

def test_r2_red_below_50_percent_of_dc_value():
    # DC value = 8 kanal x 1,200,000 = 9,600,000. Declare 2,000,000 = 20.8%.
    r = evaluate(ctx(declared_price_pkr=Decimal("2000000")))
    reds = [f for f in find(r, "R2") if f.severity is Severity.RED]
    assert reds
    assert "benami" in reds[0].detail.lower()


def test_r2_amber_between_50_and_70_percent():
    r = evaluate(ctx(declared_price_pkr=Decimal("6000000")))   # 62.5%
    assert [f for f in find(r, "R2") if f.severity is Severity.AMBER]
    assert not [f for f in find(r, "R2") if f.severity is Severity.RED]


def test_r2_silent_at_full_value():
    assert not [f for f in find(evaluate(ctx()), "R2")
                if f.severity in (Severity.RED, Severity.AMBER)]


def test_r2_skipped_for_inheritance():
    r = evaluate(ctx(transaction_type=TransactionType.INHERITANCE,
                     declared_price_pkr=Decimal("0")))
    assert not find(r, "R2")


def test_r2_flags_a_missing_rate_as_a_data_gap_not_fraud():
    """A rule that shouts fraud at a data gap gets switched off."""
    r = evaluate(ctx(dc_rate_per_kanal=None))
    assert [f for f in find(r, "R2") if f.severity is Severity.AMBER]
    assert not [f for f in find(r, "R2") if f.severity is Severity.RED]


# --------------------------------------------------------------------------
# R3 — seller viability
# --------------------------------------------------------------------------

def test_r3_blocks_a_deceased_seller():
    r = evaluate(ctx(sellers=(Party(name="Late Mir Afzal", cnic="1720111122233",
                                    nadra_status=NadraStatus.DECEASED),)))
    reds = [f for f in find(r, "R3") if f.severity is Severity.RED]
    assert reds and "deceased" in reds[0].title.lower()


def test_r3_flags_extreme_age():
    r = evaluate(ctx(sellers=(Party(name="Elderly", cnic="1730112345671", age=95,
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=False),)))
    assert [f for f in find(r, "R3") if f.severity is Severity.RED]


def test_r3_extreme_age_with_biometric_downgrades_to_amber():
    r = evaluate(ctx(sellers=(Party(name="Elderly", cnic="1730112345671", age=95,
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=True),)))
    assert [f for f in find(r, "R3") if f.severity is Severity.AMBER]
    assert not [f for f in find(r, "R3") if f.severity is Severity.RED]


def test_r3_unverified_cnic_is_amber_not_red():
    r = evaluate(ctx(sellers=(Party(name="X", cnic="1730112345671",
                                    nadra_status=NadraStatus.UNVERIFIED),)))
    assert [f for f in find(r, "R3") if f.severity is Severity.AMBER]


# --------------------------------------------------------------------------
# R4 — power of attorney
# --------------------------------------------------------------------------

def test_r4_blocks_unverified_poa():
    r = evaluate(ctx(sellers=(Party(name="Nasreen Bibi", cnic="1730144455566",
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=True, acting_via_poa=True,
                                    poa_biometric_verified=False,
                                    poa_registered=True),)))
    assert [f for f in find(r, "R4") if f.severity is Severity.RED]


def test_r4_passes_a_verified_poa():
    r = evaluate(ctx(sellers=(Party(name="Nasreen Bibi", cnic="1730144455566",
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=True, acting_via_poa=True,
                                    poa_biometric_verified=True,
                                    poa_registered=True),)))
    assert not [f for f in find(r, "R4") if f.severity is Severity.RED]


# --------------------------------------------------------------------------
# R5 — latha identity drift (and the guard that it never blocks alone)
# --------------------------------------------------------------------------

def test_r5_never_returns_red_under_any_circumstances():
    """Caste/clan is a weak identifier. It must never block by itself."""
    worst = ctx(
        latha=LathaRecord(owner_name="Totally Different Person",
                          father_name="Someone Else", qaum="Gujjar",
                          mouza="Nowhere", area_kanal=Decimal("1")),
        sellers=(Party(name="Gul Muhammad Khan", father_name="Sher Dil Khan",
                       qaum="Khattak", cnic="1730112345671",
                       nadra_status=NadraStatus.LIVE, biometric_verified=True),),
    )
    r = evaluate(worst)
    assert not [f for f in find(r, "R5") if f.severity is Severity.RED]


def test_r5_amber_when_name_does_not_match_latha():
    r = evaluate(ctx(latha=LathaRecord(owner_name="Rozi Khan", qaum="Yousafzai",
                                       mouza="Landi Arbab", area_kanal=Decimal("8"))))
    assert [f for f in find(r, "R5") if f.severity is Severity.AMBER]


def test_r5_quiet_when_record_matches():
    r = evaluate(ctx(latha=LathaRecord(owner_name="Gul Muhammad Khan",
                                       father_name="Sher Dil Khan", qaum="Khattak",
                                       mouza="Landi Arbab", area_kanal=Decimal("8"))))
    assert not [f for f in find(r, "R5") if f.severity is Severity.AMBER]


def test_r5_detects_area_inflation():
    r = evaluate(ctx(area_kanal=Decimal("12"),
                     latha=LathaRecord(owner_name="Gul Muhammad Khan",
                                       father_name="Sher Dil Khan", qaum="Khattak",
                                       mouza="Landi Arbab", area_kanal=Decimal("8"))))
    assert any("Area differs" in f.title for f in find(r, "R5"))


def test_name_similarity_folds_transliteration():
    assert name_similarity("ګل محمد", "گل محمد") > Decimal("0.8")
    assert name_similarity("Gul Muhammad Khan", "Gul Mohammad Khan") > Decimal("0.8")


def test_name_similarity_separates_different_people():
    assert name_similarity("Gul Muhammad Khan", "Sher Dil Khan") < Decimal("0.5")


def test_normalise_name_strips_honorifics_and_relation_markers():
    # "Muhammad" and "Khan" are deliberately treated as noise: KP records
    # routinely drop or add them, and they carry almost no discriminating power.
    assert normalise_name("Haji Gul Sher S/O Rozi") == "haji gul sher rozi"
    assert normalise_name("Zamina Bibi D/O Bakht") == "zamina bibi bakht"
    assert normalise_name("Late Mir Afzal Khan") == "mir afzal"


# --------------------------------------------------------------------------
# R6 — ground verification / geofence
# --------------------------------------------------------------------------

def test_r6_requires_a_site_visit_for_a_sale():
    r = evaluate(ctx(ground=None))
    assert [f for f in find(r, "R6") if f.severity is Severity.RED]


def test_r6_does_not_require_a_visit_for_inheritance():
    r = evaluate(ctx(transaction_type=TransactionType.INHERITANCE, ground=None))
    assert not [f for f in find(r, "R6") if f.severity is Severity.RED]


def test_r6_accepts_a_good_visit():
    r = evaluate(ctx(ground=GroundEvidence(
        verified=True, latitude=34.045, longitude=71.625, accuracy_m=6.0,
        captured_at=NOW - timedelta(minutes=20), synced_at=NOW - timedelta(minutes=15),
        inside_mouza=True, distance_to_boundary_m=0.0, photo_sha256="a" * 64,
        photo_exif_datetime=NOW - timedelta(minutes=20), photo_from_camera=True,
        witness_cnics=("h1", "h2"), witnesses_biometric=(True, True),
        device_id="d1", trust_score=95)))
    assert not [f for f in find(r, "R6") if f.severity is Severity.RED]


def test_r6_flags_mock_gps():
    r = evaluate(ctx(ground=GroundEvidence(
        verified=True, latitude=34.045, longitude=71.625, accuracy_m=6.0,
        captured_at=NOW, synced_at=NOW, inside_mouza=True, photo_sha256="a" * 64,
        photo_from_camera=True, witness_cnics=("h1", "h2"),
        witnesses_biometric=(True, True), mock_location_suspected=True,
        trust_score=10)))
    assert any("Mock" in f.title for f in find(r, "R6"))


def test_r6_flags_a_fix_outside_the_mouza():
    r = evaluate(ctx(ground=GroundEvidence(
        verified=True, latitude=35.5, longitude=74.0, accuracy_m=6.0,
        captured_at=NOW, synced_at=NOW, inside_mouza=False,
        distance_to_boundary_m=180000.0, photo_sha256="a" * 64,
        photo_from_camera=True, witness_cnics=("h1", "h2"),
        witnesses_biometric=(True, True), trust_score=60)))
    assert any("outside" in f.title.lower() for f in find(r, "R6"))


def test_r6_flags_a_long_offline_window():
    r = evaluate(ctx(ground=GroundEvidence(
        verified=True, latitude=34.045, longitude=71.625, accuracy_m=6.0,
        captured_at=NOW - timedelta(hours=200), synced_at=NOW, inside_mouza=True,
        photo_sha256="a" * 64, photo_from_camera=True,
        witness_cnics=("h1", "h2"), witnesses_biometric=(True, True),
        trust_score=60)))
    assert [f for f in find(r, "R6") if f.severity is Severity.RED]


# --------------------------------------------------------------------------
# R7 — double sale / shares
# --------------------------------------------------------------------------

def test_r7_blocks_conveying_more_than_held():
    r = evaluate(ctx(seller_held_share=Decimal("0.25"),
                     sellers=(Party(name="A", cnic="1730112345671", share_numerator=1,
                                    share_denominator=1,
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=True),)))
    assert [f for f in find(r, "R7") if f.severity is Severity.RED]


def test_r7_allows_conveying_exactly_what_is_held():
    r = evaluate(ctx(seller_held_share=Decimal("1"),
                     sellers=(Party(name="A", cnic="1730112345671", share_numerator=1,
                                    share_denominator=1,
                                    nadra_status=NadraStatus.LIVE,
                                    biometric_verified=True),)))
    assert not [f for f in find(r, "R7") if f.severity is Severity.RED]


def test_r7_detects_oversubscribed_khasra():
    r = evaluate(ctx(khasra_shares_total=Decimal("1.5")))
    assert any("100%" in f.title for f in find(r, "R7"))


def test_r7_does_not_cry_wolf_when_shares_are_unknown():
    """Empty share register = no data, not a double sale."""
    r = evaluate(ctx(seller_held_share=None))
    r7 = find(r, "R7")
    assert r7 and all(f.severity is Severity.INFO for f in r7)


# --------------------------------------------------------------------------
# Scoring, config, privacy
# --------------------------------------------------------------------------

def test_score_is_bounded_and_transparent():
    r = evaluate(ctx(prior_mutations=(NOW - timedelta(days=2),),
                     declared_price_pkr=Decimal("1000")))
    assert 0 <= r.score <= 100
    assert r.red_count >= 2
    assert r.score >= 2 * DEFAULT_CONFIG.weight_red


def test_config_fingerprint_changes_with_thresholds():
    assert DEFAULT_CONFIG.fingerprint() != \
        DEFAULT_CONFIG.replace(undervaluation_red_pct=Decimal("10")).fingerprint()


def test_unknown_rule_raises():
    with pytest.raises(KeyError):
        evaluate(ctx(), rules=["R99"])


def test_cnic_is_never_exposed_in_evidence():
    r = evaluate(ctx(sellers=(Party(name="X", cnic="1730112345671", age=95,
                                    nadra_status=NadraStatus.LIVE),)))
    blob = str([f.to_dict() for f in r.findings])
    assert "1730112345671" not in blob
    assert mask_cnic("1730112345671") == "17301-*******-1"
