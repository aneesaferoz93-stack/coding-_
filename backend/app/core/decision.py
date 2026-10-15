"""
decision.py — turn findings into an operational decision, gated by rollout phase.

This is where the Punjab lesson is written into code.

Punjab made the *legal* effect of a new digital instrument change instantly and
province-wide: on 28 April 2026 the Board of Revenue suspended the issue of
Fard in Sahiwal and declared the Green Property Certificate the "sole valid
document"; by 1 July it was mandatory across the province. By 11 July the
suspension had been partially reversed for non-digitised areas, and on 28
August the Punjab Assembly suspended the whole scheme for six months. The
technology may or may not have been sound. The *cutover* was not: a document
that 100% of transactions depended on was withdrawn faster than the
alternative could serve 100% of cases, and the reversal cost the reform its
credibility.

So the engine's verdict and the system's legal behaviour are separate things:

    severity (what the evidence says)   x   phase (how much power we have)
                                        =   decision (what actually happens)

During Phase 0 and Phase 1 the system issues an *opinion*, never a refusal.
The manual register keeps running. Nothing a citizen is legally entitled to
today becomes unavailable tomorrow because of this system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .fraud_rules import EvaluationResult, Finding, Severity

POLICY_VERSION = "1.0.0"


class Phase(str, Enum):
    """The four gates in docs/02-rollout-plan.md."""

    PHASE_0_PILOT = "PHASE_0_PILOT"
    PHASE_1_DUAL_RUN = "PHASE_1_DUAL_RUN"
    PHASE_2_ENFORCE = "PHASE_2_ENFORCE"
    PHASE_3_LEGAL = "PHASE_3_LEGAL"


class Decision(str, Enum):
    CLEAR = "CLEAR"          # proceed
    HOLD = "HOLD"            # proceed only after a named human action
    BLOCK = "BLOCK"          # do not proceed
    ADVISORY = "ADVISORY"    # proceed, but the flag is recorded (Phase 0/1)


@dataclass(frozen=True)
class PhasePolicy:
    """How findings translate into action while a given phase is active."""

    phase: Phase
    label_en: str
    # A RED finding becomes this decision.
    red_decision: Decision
    # An AMBER finding becomes this decision.
    amber_decision: Decision
    # Is the decision legally binding on the revenue officer? If False the
    # officer may proceed, and the override is itself recorded in the ledger.
    binding: bool
    # If True, ground verification is a hard requirement for SALE/GIFT/etc.
    ground_mandatory: bool
    # Any decision with severity RED is routed here for human review.
    escalation_role: str

    def describe_en(self) -> str:
        binding = "binding" if self.binding else "advisory (officer may override, on the record)"
        return (f"{self.phase.value} — {self.label_en}. RED → {self.red_decision.value}, "
                f"AMBER → {self.amber_decision.value}. Decisions are {binding}. "
                f"Escalation to: {self.escalation_role}.")


PHASE_POLICIES: dict[Phase, PhasePolicy] = {
    Phase.PHASE_0_PILOT: PhasePolicy(
        phase=Phase.PHASE_0_PILOT,
        label_en="Pilot — 3 districts, observation only",
        red_decision=Decision.ADVISORY,
        amber_decision=Decision.ADVISORY,
        binding=False,
        ground_mandatory=False,
        escalation_role="Tehsildar (pilot cell)",
    ),
    Phase.PHASE_1_DUAL_RUN: PhasePolicy(
        phase=Phase.PHASE_1_DUAL_RUN,
        label_en="Dual-run — manual register continues, 12 months",
        red_decision=Decision.HOLD,
        amber_decision=Decision.HOLD,
        binding=False,          # officer can override — but it lands in the ledger
        ground_mandatory=True,
        escalation_role="Tehsildar / Deputy Commissioner",
    ),
    Phase.PHASE_2_ENFORCE: PhasePolicy(
        phase=Phase.PHASE_2_ENFORCE,
        label_en="Enforcement — cleared districts only",
        red_decision=Decision.BLOCK,
        amber_decision=Decision.HOLD,
        binding=True,
        ground_mandatory=True,
        escalation_role="Deputy Commissioner / Board of Revenue",
    ),
    Phase.PHASE_3_LEGAL: PhasePolicy(
        phase=Phase.PHASE_3_LEGAL,
        label_en="Legal cover — hash-verified record admissible",
        red_decision=Decision.BLOCK,
        amber_decision=Decision.HOLD,
        binding=True,
        ground_mandatory=True,
        escalation_role="Board of Revenue / Revenue Courts",
    ),
}


@dataclass(slots=True)
class RequiredAction:
    action: str
    owner_role: str
    sla_hours: int | None = None


@dataclass(slots=True)
class ClearanceDecision:
    """What the revenue officer is told, and what e-Fard is told to do."""

    clearance_id: str
    mutation_id: str
    decision: Decision
    phase: Phase
    binding: bool
    severity: Severity
    score: int
    reasons: list[str] = field(default_factory=list)
    required_actions: list[RequiredAction] = field(default_factory=list)
    escalation_role: str | None = None
    expires_at: datetime | None = None
    ledger_seq: int | None = None
    ledger_hash: str | None = None
    verify_url: str | None = None
    policy_version: str = POLICY_VERSION
    config_fingerprint: str = ""
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clearance_id": self.clearance_id,
            "mutation_id": self.mutation_id,
            "decision": self.decision.value,
            "phase": self.phase.value,
            "binding": self.binding,
            "severity": self.severity.value,
            "score": self.score,
            "reasons": self.reasons,
            "required_actions": [
                {"action": a.action, "owner_role": a.owner_role, "sla_hours": a.sla_hours}
                for a in self.required_actions
            ],
            "escalation_role": self.escalation_role,
            "expires_at": self.expires_at,
            "ledger_seq": self.ledger_seq,
            "ledger_hash": self.ledger_hash,
            "verify_url": self.verify_url,
            "policy_version": self.policy_version,
            "config_fingerprint": self.config_fingerprint,
            "decided_at": self.decided_at,
        }


# Findings that are so severe they are escalated regardless of phase, because
# letting them through even in an advisory phase creates an irreversible harm
# (land leaves a dead person's estate, or is conveyed twice).
IRREVERSIBLE_RULE_IDS = {"R3", "R7"}


def decide(
    evaluation: EvaluationResult,
    *,
    phase: Phase,
    clearance_id: str,
    base_url: str | None = None,
    hold_ttl_hours: int = 72,
) -> ClearanceDecision:
    """Apply the active phase policy to an evaluation."""
    policy = PHASE_POLICIES[phase]

    if evaluation.red_count:
        base = policy.red_decision
    elif evaluation.amber_count:
        base = policy.amber_decision
    else:
        base = Decision.CLEAR

    reasons = [f"{f.rule_id} [{f.severity.value}] {f.title}" for f in evaluation.findings]

    actions: list[RequiredAction] = []
    for f in evaluation.findings:
        if f.severity is Severity.INFO:
            continue
        sla = 24 if f.severity is Severity.RED else 72
        owner = policy.escalation_role if f.severity is Severity.RED else "Patwari"
        if f.recommended_action:
            actions.append(RequiredAction(action=f.recommended_action,
                                          owner_role=owner, sla_hours=sla))

    # Irreversible-harm findings never come back as a bare ADVISORY, even in
    # Phase 0 — they always demand a named human sign-off.
    if base is Decision.ADVISORY and any(
        f.severity is Severity.RED and f.rule_id in IRREVERSIBLE_RULE_IDS
        for f in evaluation.findings
    ):
        base = Decision.HOLD
        actions.append(RequiredAction(
            action=("Irreversible-harm flag: a Tehsildar must review and sign the "
                    "override before this mutation is attested."),
            owner_role="Tehsildar",
            sla_hours=24,
        ))

    escalation = None
    if base in (Decision.HOLD, Decision.BLOCK):
        escalation = policy.escalation_role

    verify_url = None
    if base_url and evaluation.findings:
        verify_url = f"{base_url.rstrip('/')}/verify/{clearance_id}"

    return ClearanceDecision(
        clearance_id=clearance_id,
        mutation_id=evaluation.mutation_id,
        decision=base,
        phase=phase,
        binding=policy.binding and base is Decision.BLOCK,
        severity=evaluation.max_severity,
        score=evaluation.score,
        reasons=reasons,
        required_actions=actions,
        escalation_role=escalation,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=hold_ttl_hours)
                    if base is Decision.HOLD else None),
        verify_url=verify_url,
        config_fingerprint=evaluation.config_fingerprint,
    )


def summarise_findings(findings: list[Finding]) -> dict[str, int]:
    return {
        "RED": sum(1 for f in findings if f.severity is Severity.RED),
        "AMBER": sum(1 for f in findings if f.severity is Severity.AMBER),
        "INFO": sum(1 for f in findings if f.severity is Severity.INFO),
    }
