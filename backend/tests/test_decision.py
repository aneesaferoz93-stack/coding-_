"""Phase gating — the part that stops us repeating Punjab's cutover mistake."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.decision import (
    PHASE_POLICIES,
    Decision,
    Phase,
    decide,
)
from app.core.fraud_rules import (
    DEFAULT_CONFIG,
    EvaluationResult,
    Finding,
    MutationContext,
    NadraStatus,
    Party,
    Severity,
    TransactionType,
    evaluate,
)

NOW = datetime(2026, 10, 15, 10, 0, 0, tzinfo=timezone.utc)


def clean_ctx() -> MutationContext:
    from app.core.fraud_rules import GroundEvidence
    return MutationContext(
        mutation_id="M1", district_code="PESHAWAR", tehsil_code="PESHAWAR",
        mouza_code="MZ-PSH-001", mouza_name="Landi Arbab", khasra_no="441",
        area_kanal=Decimal("8"), transaction_type=TransactionType.SALE,
        declared_price_pkr=Decimal("12000000"),
        dc_rate_per_kanal=Decimal("1200000"), registered_at=NOW,
        sellers=(Party(name="A", cnic="1730112345671", nadra_status=NadraStatus.LIVE,
                       biometric_verified=True),),
        buyers=(Party(name="B", cnic="1730198765432", nadra_status=NadraStatus.LIVE,
                      biometric_verified=True),),
        ground=GroundEvidence(verified=True, latitude=34.045, longitude=71.625,
                              accuracy_m=6, captured_at=NOW - timedelta(minutes=20),
                              synced_at=NOW, inside_mouza=True,
                              distance_to_boundary_m=0.0, photo_sha256="a" * 64,
                              photo_exif_datetime=NOW, photo_from_camera=True,
                              witness_cnics=("h1", "h2"),
                              witnesses_biometric=(True, True), trust_score=95),
        seller_held_share=Decimal("1"))


def dead_seller_ctx() -> MutationContext:
    return MutationContext(
        mutation_id="M2", district_code="SWAT", tehsil_code="BABUZAI",
        mouza_code="MZ-SWT-001", mouza_name="Saidu Sharif", khasra_no="88",
        area_kanal=Decimal("6"), transaction_type=TransactionType.INHERITANCE,
        declared_price_pkr=Decimal("0"), dc_rate_per_kanal=Decimal("900000"),
        registered_at=NOW,
        sellers=(Party(name="Late Mir Afzal", cnic="1720111122233",
                       nadra_status=NadraStatus.DECEASED),),
        buyers=(Party(name="B", cnic="1730198765432",
                      nadra_status=NadraStatus.LIVE, biometric_verified=True),))


def test_clean_mutation_is_clear_in_every_phase():
    ev = evaluate(clean_ctx())
    assert ev.red_count == 0 and ev.amber_count == 0
    for phase in Phase:
        d = decide(ev, phase=phase, clearance_id="c")
        assert d.decision is Decision.CLEAR


def test_a_red_flag_does_not_block_in_phase_0():
    """Phase 0 is observation. It must not stop a single transaction."""
    ev = evaluate(dead_seller_ctx())
    assert ev.red_count >= 1
    d = decide(ev, phase=Phase.PHASE_0_PILOT, clearance_id="c")
    assert d.decision is not Decision.BLOCK
    assert d.binding is False


def test_irreversible_findings_always_demand_a_human():
    """Even in Phase 0, a dead seller or a double sale cannot be a bare ADVISORY."""
    ev = evaluate(dead_seller_ctx())
    d = decide(ev, phase=Phase.PHASE_0_PILOT, clearance_id="c")
    assert d.decision is Decision.HOLD
    assert any("Tehsildar" in a.owner_role for a in d.required_actions)


def test_phase_1_holds_but_is_overridable():
    ev = evaluate(dead_seller_ctx())
    d = decide(ev, phase=Phase.PHASE_1_DUAL_RUN, clearance_id="c")
    assert d.decision is Decision.HOLD
    assert d.binding is False       # officer may proceed — on the record


def test_phase_2_blocks_and_is_binding():
    ev = evaluate(dead_seller_ctx())
    d = decide(ev, phase=Phase.PHASE_2_ENFORCE, clearance_id="c")
    assert d.decision is Decision.BLOCK
    assert d.binding is True


def test_amber_becomes_hold_not_block_in_enforcement():
    """An AMBER flag asks a human a question; it does not refuse service."""
    ev = EvaluationResult(mutation_id="M3", findings=[
        Finding(rule_id="R5", rule_name="x", severity=Severity.AMBER,
                title="t", detail="d", recommended_action="Check the latha.")],
        score=18, red_count=0, amber_count=1)
    d = decide(ev, phase=Phase.PHASE_2_ENFORCE, clearance_id="c")
    assert d.decision is Decision.HOLD
    assert d.binding is False


def test_every_phase_policy_is_defined():
    assert set(PHASE_POLICIES) == set(Phase)


def test_hold_carries_an_expiry_and_an_escalation():
    ev = evaluate(dead_seller_ctx())
    d = decide(ev, phase=Phase.PHASE_1_DUAL_RUN, clearance_id="c")
    assert d.expires_at is not None
    assert d.escalation_role


def test_decision_records_the_config_fingerprint():
    ev = evaluate(clean_ctx(), DEFAULT_CONFIG)
    d = decide(ev, phase=Phase.PHASE_1_DUAL_RUN, clearance_id="c")
    assert d.config_fingerprint == DEFAULT_CONFIG.fingerprint()
