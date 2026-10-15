"""
fraud_rules.py — the KP Land Record fraud rule engine.

This module is the reason this system exists. e-Fard *issues* documents; it does
not ask whether the mutation behind the document is real. Everything here runs
on data that already exists inside the Revenue Department (LRMIS mutation
history, the DC rate table, NADRA, the old manual ``latha``), so no new
bureaucracy is required to feed it.

Design principles
-----------------
1. **Explainable, not magic.** Every finding carries the numbers that produced
   it and the name of the rule. A Tehsildar must be able to read a flag and
   argue with it. There is no ML black box in the decision path.
2. **Rules are data.** Thresholds live in :class:`RuleConfig`, are versioned,
   and can be tuned per district during Phase 1 without a code release.
3. **Severity != decision.** A rule declares how suspicious something is; the
   rollout *phase* decides whether a RED flag blocks or merely holds. That
   separation is what stops us repeating Punjab's mistake (see
   docs/03-punjab-postmortem.md).
4. **Never block on a weak identifier alone.** Caste/``qaum``, name spelling and
   old-record fields are advisory. They raise AMBER. They never raise RED on
   their own, because KP's own manual records are full of transliteration
   noise and blocking a widow's inheritance on a spelling difference is worse
   than letting a fraudster through to a human review queue.

Severity model
--------------
``RED``    — strong signal of fraud; in enforcement mode this blocks issuance.
``AMBER``  — suspicious; requires a named human action before proceeding.
``INFO``   — recorded for pattern analysis; no action.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from .canonical import canonical_sha256

RULE_ENGINE_VERSION = "1.3.0"


# --------------------------------------------------------------------------
# Enumerations & config
# --------------------------------------------------------------------------

class Severity(str, Enum):
    RED = "RED"
    AMBER = "AMBER"
    INFO = "INFO"


class TransactionType(str, Enum):
    SALE = "SALE"
    GIFT = "GIFT"
    INHERITANCE = "INHERITANCE"
    EXCHANGE = "EXCHANGE"
    PARTITION = "PARTITION"
    MORTGAGE = "MORTGAGE"
    REDEMPTION = "REDEMPTION"
    COURT_DECREE = "COURT_DECREE"
    GOVT_ACQUISITION = "GOVT_ACQUISITION"
    CORRECTION = "CORRECTION"


class NadraStatus(str, Enum):
    LIVE = "LIVE"
    DECEASED = "DECEASED"
    NOT_FOUND = "NOT_FOUND"
    BLOCKED = "BLOCKED"          # CNIC blocked / cancelled
    UNVERIFIED = "UNVERIFIED"    # never queried — the honest default


@dataclass(frozen=True)
class RuleConfig:
    """Every threshold in the engine. Versioned so audits can be reproduced."""

    version: str = RULE_ENGINE_VERSION

    # Rule 1 — rapid re-mutation
    rapid_remutation_window_days: int = 30
    rapid_remutation_max: int = 1            # >1 mutation in window => RED
    rapid_remutation_watch_days: int = 60    # softer window
    rapid_remutation_watch_max: int = 2

    # Rule 2 — undervaluation vs DC table rate
    undervaluation_red_pct: Decimal = Decimal("50")    # <50% of DC value => RED
    undervaluation_amber_pct: Decimal = Decimal("70")  # <70% => AMBER
    undervaluation_min_value_pkr: Decimal = Decimal("100000")  # ignore trivial sums

    # Rule 3 — seller viability
    seller_age_red: int = 90
    seller_age_amber: int = 80

    # Rule 4 — power of attorney
    poa_requires_biometric: bool = True

    # Rule 5 — identity drift vs old latha
    latha_match_min_score: Decimal = Decimal("0.60")   # 0..1 fuzzy name match
    latha_area_tolerance_pct: Decimal = Decimal("10")

    # Extended rules
    patwari_daily_mutation_amber: int = 12
    witness_required: int = 2
    ground_required_for: tuple[str, ...] = (
        TransactionType.SALE.value,
        TransactionType.GIFT.value,
        TransactionType.PARTITION.value,
        TransactionType.EXCHANGE.value,
    )
    geo_fence_max_distance_m: int = 250       # metres from mouza boundary
    offline_sync_warn_hours: int = 24
    offline_sync_red_hours: int = 96

    # Scoring
    weight_red: int = 45
    weight_amber: int = 18
    weight_info: int = 3

    def replace(self, **kwargs: Any) -> RuleConfig:
        return replace(self, **kwargs)

    def fingerprint(self) -> str:
        """Hash of the active configuration — recorded with every evaluation."""
        return canonical_sha256({
            "engine": self.version,
            "rapid": [self.rapid_remutation_window_days, self.rapid_remutation_max,
                      self.rapid_remutation_watch_days, self.rapid_remutation_watch_max],
            "undervalue": [str(self.undervaluation_red_pct), str(self.undervaluation_amber_pct),
                           str(self.undervaluation_min_value_pkr)],
            "seller": [self.seller_age_red, self.seller_age_amber],
            "poa": self.poa_requires_biometric,
            "latha": [str(self.latha_match_min_score), str(self.latha_area_tolerance_pct)],
            "extended": [self.patwari_daily_mutation_amber, self.witness_required,
                         self.geo_fence_max_distance_m, self.offline_sync_warn_hours,
                         self.offline_sync_red_hours],
            "weights": [self.weight_red, self.weight_amber, self.weight_info],
        })


DEFAULT_CONFIG = RuleConfig()


# --------------------------------------------------------------------------
# Input model
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Party:
    name: str
    cnic: str | None = None
    father_name: str | None = None
    qaum: str | None = None            # caste / tribe as recorded in the latha
    age: int | None = None
    nadra_status: NadraStatus = NadraStatus.UNVERIFIED
    biometric_verified: bool = False
    acting_via_poa: bool = False
    poa_biometric_verified: bool = False
    poa_attorney_cnic: str | None = None
    poa_registered: bool = False
    resident_mouza: str | None = None
    share_numerator: int | None = None     # e.g. 1 of 4 => 1/4
    share_denominator: int | None = None

    def share(self) -> Decimal | None:
        if self.share_numerator is None or not self.share_denominator:
            return None
        return Decimal(self.share_numerator) / Decimal(self.share_denominator)


@dataclass(slots=True)
class LathaRecord:
    """The pre-computerisation manual register — our ground truth for identity."""

    owner_name: str
    father_name: str | None = None
    qaum: str | None = None
    mouza: str | None = None
    khasra_no: str | None = None
    area_kanal: Decimal | None = None
    jamabandi_year: int | None = None
    scanned_page: str | None = None


@dataclass(slots=True)
class GroundEvidence:
    """What the Patwari's app captured on site (see core/ground_trust.py)."""

    verified: bool = False
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    captured_at: datetime | None = None
    synced_at: datetime | None = None
    inside_mouza: bool | None = None
    distance_to_boundary_m: float | None = None
    photo_sha256: str | None = None
    photo_exif_datetime: datetime | None = None
    photo_from_camera: bool = False
    witness_cnics: tuple[str, ...] = ()
    witnesses_biometric: tuple[bool, ...] = ()
    device_id: str | None = None
    mock_location_suspected: bool = False
    trust_score: int | None = None


@dataclass(slots=True)
class MutationContext:
    """Everything the engine needs to judge one proposed mutation."""

    mutation_id: str
    district_code: str
    tehsil_code: str
    mouza_code: str
    mouza_name: str
    khasra_no: str
    khewat_no: str | None = None
    area_kanal: Decimal = Decimal("0")
    transaction_type: TransactionType = TransactionType.SALE
    declared_price_pkr: Decimal = Decimal("0")
    dc_rate_per_kanal: Decimal | None = None
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    patwari_id: str | None = None
    patwari_mutations_today: int = 0

    sellers: tuple[Party, ...] = ()
    buyers: tuple[Party, ...] = ()

    # Rule 1 inputs: prior mutations on this exact khasra
    prior_mutations: tuple[datetime, ...] = ()

    # Rule 7 inputs: ownership chain
    seller_held_share: Decimal | None = None     # share seller actually owns today
    khasra_shares_total: Decimal | None = None   # sum of all recorded shares

    # Rule 5 input
    latha: LathaRecord | None = None

    # Rule 6/9 inputs
    ground: GroundEvidence | None = None

    # Free-form extras (e-registry ref, court case no, etc.)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def dc_value_pkr(self) -> Decimal | None:
        if self.dc_rate_per_kanal is None:
            return None
        return (self.dc_rate_per_kanal * self.area_kanal).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------
# Output model
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Finding:
    rule_id: str
    rule_name: str
    severity: Severity
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""
    legal_basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "recommended_action": self.recommended_action,
            "legal_basis": self.legal_basis,
        }


@dataclass(slots=True)
class EvaluationResult:
    mutation_id: str
    findings: list[Finding] = field(default_factory=list)
    score: int = 0
    red_count: int = 0
    amber_count: int = 0
    config_fingerprint: str = ""
    engine_version: str = RULE_ENGINE_VERSION
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def max_severity(self) -> Severity:
        if self.red_count:
            return Severity.RED
        if self.amber_count:
            return Severity.AMBER
        return Severity.INFO

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "engine_version": self.engine_version,
            "config_fingerprint": self.config_fingerprint,
            "evaluated_at": self.evaluated_at,
            "score": self.score,
            "red_count": self.red_count,
            "amber_count": self.amber_count,
            "max_severity": self.max_severity.value,
            "findings": [f.to_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z\u0600-\u06FF\u0750-\u077F ]+")
_URDU_NOISE = {
    "son", "s/o", "d/o", "w/o", "bin", "bint", "walad", "late", "mr", "mrs",
    "muhammad", "mohammad", "mohd", "kh", "khan", "sahib",
}
# Transliteration equivalence classes seen across KP's manual records.
# Transliteration equivalence classes seen across KP's manual records.
# Urdu/Pashto records write the same name with ک/ك, ی/ي, ہ/ه interchangeably,
# so Rule 5 normalises before comparing or it flags half the district.
_TRANSLIT = str.maketrans({
    'ک': 'ك', 'ی': 'ي', 'ے': 'ي', 'ۍ': 'ي', 'ئ': 'ي', 'ہ': 'ه',
    'ۂ': 'ه', 'ھ': 'ه', 'ں': 'ن', 'ٹ': 'ت', 'ڈ': 'د', 'ڑ': 'ر',
    'ژ': 'ز', 'چ': 'چ', 'ث': 'س', 'ص': 'س', 'ض': 'ز', 'ط': 'ت',
    'ظ': 'ز', 'ع': 'ا', 'غ': 'گ', 'ؤ': 'و',
})


_RELATION_MARKER = re.compile(r"\b[sdw]\s*/\s*o\b", re.IGNORECASE)


def normalise_name(value: str | None) -> str:
    """Fold a name for comparison: strip diacritics, transliteration variants
    and honorifics. Urdu/Pashto name matching is genuinely hard and this is
    deliberately conservative — it is used for AMBER-grade evidence only."""
    if not value:
        return ""
    text = _RELATION_MARKER.sub(" ", value)      # s/o, d/o, w/o
    text = _ARABIC_DIACRITICS.sub("", text).strip().lower()
    text = text.translate(_TRANSLIT)
    text = _NON_ALNUM.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _URDU_NOISE]
    return " ".join(tokens)


def name_similarity(a: str | None, b: str | None) -> Decimal:
    """Cheap, dependency-free fuzzy match in [0,1].

    Sequence ratio on normalised tokens. Returns 1.0 for an exact normalised
    match and degrades gracefully. Not a substitute for a human reading the
    scanned latha — which is why it only ever produces AMBER.
    """
    na, nb = normalise_name(a), normalise_name(b)
    if na == nb:
        return Decimal("1")
    if not na or not nb:
        return Decimal("0")
    import difflib

    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    # Token-level containment catches "Ahmad Khan" vs "Ahmad Khan Gul".
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    score = max(ratio, jaccard)
    return Decimal(str(round(score, 3)))


def valid_cnic(cnic: str | None) -> bool:
    """NADRA CNIC is 13 digits, unformatted."""
    if not cnic:
        return False
    digits = re.sub(r"\D", "", cnic)
    return len(digits) == 13


def mask_cnic(cnic: str | None) -> str:
    """Never log or emit a full CNIC (see docs/05-threat-model.md)."""
    if not cnic:
        return ""
    digits = re.sub(r"\D", "", cnic)
    return f"{digits[:5]}-*******-{digits[-1]}" if len(digits) == 13 else "****"


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    if not whole:
        return Decimal("0")
    return (part / whole * Decimal("100")).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------
# Rule implementations
# --------------------------------------------------------------------------
# Each rule is a pure function: (context, config) -> list[Finding]

def rule_r1_rapid_remutation(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R1 — Same Khasra mutated more than once in 30 days.

    The single highest-yield land-fraud signal in Pakistan: a fraudulent
    mutation is usually a *chain* (A→B→C) laid down quickly so that by the time
    the true owner looks, the paper trail shows a plausible third party in
    possession. Legitimate families do not sell the same survey number twice in
    a month.
    """
    if not ctx.prior_mutations:
        return []

    now = ctx.registered_at
    window = timedelta(days=cfg.rapid_remutation_window_days)
    in_window = [d for d in ctx.prior_mutations if now - window <= d <= now]

    findings: list[Finding] = []
    if len(in_window) >= cfg.rapid_remutation_max:
        findings.append(Finding(
            rule_id="R1",
            rule_name="Rapid re-mutation",
            severity=Severity.RED,
            title=f"Khasra {ctx.khasra_no} mutated {len(in_window) + 1}x in "
                  f"{cfg.rapid_remutation_window_days} days",
            detail=("This survey number has already been mutated "
                    f"{len(in_window)} time(s) in the last "
                    f"{cfg.rapid_remutation_window_days} days. Rapid successive "
                    "mutations on one khasra are the standard method for "
                    "laundering a disputed or forged transfer through a chain of "
                    "intermediate 'owners'."),
            evidence={
                "khasra_no": ctx.khasra_no,
                "mouza": ctx.mouza_code,
                "window_days": cfg.rapid_remutation_window_days,
                "prior_mutations_in_window": len(in_window),
                "prior_mutation_dates": sorted(d.isoformat() for d in in_window),
                "total_prior_mutations": len(ctx.prior_mutations),
            },
            recommended_action=("Do not attest. Escalate to Tehsildar for a "
                                "sanctioned mutation; obtain the full chain of "
                                "prior mutation orders before proceeding."),
            legal_basis="KP Land Revenue Act s.34/35 — mutation requires enquiry into possession.",
        ))
    else:
        watch = timedelta(days=cfg.rapid_remutation_watch_days)
        in_watch = [d for d in ctx.prior_mutations if now - watch <= d <= now]
        if len(in_watch) >= cfg.rapid_remutation_watch_max:
            findings.append(Finding(
                rule_id="R1",
                rule_name="Rapid re-mutation",
                severity=Severity.AMBER,
                title=f"Khasra {ctx.khasra_no} shows {len(in_watch)} prior "
                      f"mutations in {cfg.rapid_remutation_watch_days} days",
                detail="Elevated mutation velocity on this survey number.",
                evidence={
                    "khasra_no": ctx.khasra_no,
                    "watch_window_days": cfg.rapid_remutation_watch_days,
                    "prior_mutations_in_window": len(in_watch),
                    "prior_mutation_dates": sorted(d.isoformat() for d in in_watch),
                },
                recommended_action="Verify possession on site before attestation.",
            ))
    return findings


def rule_r2_undervaluation(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R2 — Declared price below the DC (Deputy Commissioner) table rate.

    Property is registered at a *declared* value. Declaring far below the DC
    table rate is the classic benami move: the paper buyer is a front, the
    money never moves through a traceable channel, and the true beneficiary
    stays invisible.
    """
    dc_value = ctx.dc_value_pkr
    if dc_value is None or dc_value <= 0:
        return [Finding(
            rule_id="R2",
            rule_name="Undervaluation vs DC rate",
            severity=Severity.AMBER,
            title="No DC rate available for this mouza",
            detail=("The Deputy Commissioner's rate table has no entry for this "
                    "mouza, so the declared price cannot be sanity-checked. "
                    "Populate the rate table before enforcing valuation rules "
                    "here — otherwise rural mouzas get flagged for a data gap "
                    "rather than for fraud."),
            evidence={"mouza": ctx.mouza_code, "declared_price_pkr": str(ctx.declared_price_pkr)},
            recommended_action="Load the current DC rate for this mouza; re-run.",
        )]

    if ctx.transaction_type in (TransactionType.INHERITANCE,
                                TransactionType.COURT_DECREE,
                                TransactionType.GOVT_ACQUISITION,
                                TransactionType.CORRECTION):
        return []  # no consideration — valuation is meaningless

    if dc_value < cfg.undervaluation_min_value_pkr:
        return []

    ratio = _pct(ctx.declared_price_pkr, dc_value)
    price_per_kanal = (ctx.declared_price_pkr / ctx.area_kanal).quantize(Decimal("0.01")) \
        if ctx.area_kanal else None

    evidence = {
        "declared_price_pkr": str(ctx.declared_price_pkr),
        "dc_value_pkr": str(dc_value),
        "dc_rate_per_kanal": str(ctx.dc_rate_per_kanal),
        "declared_pct_of_dc_value": str(ratio),
        "area_kanal": str(ctx.area_kanal),
        "price_per_kanal": str(price_per_kanal) if price_per_kanal else None,
    }

    if ratio < cfg.undervaluation_red_pct:
        return [Finding(
            rule_id="R2",
            rule_name="Undervaluation vs DC rate",
            severity=Severity.RED,
            title=f"Declared price is {ratio}% of DC value (below "
                  f"{cfg.undervaluation_red_pct}%)",
            detail=(f"The transaction declares Rs {ctx.declared_price_pkr:,} against a "
                    f"DC table value of Rs {dc_value:,}. A consideration this far "
                    "below the official rate is the signature of a benami / "
                    "paper transfer where no real money changes hands and the "
                    "named buyer is a front."),
            evidence=evidence,
            recommended_action=("Require proof of payment trail (bank instrument, "
                                "not cash), re-verify both parties biometrically, "
                                "and record the source of funds before attestation."),
            legal_basis="Benami Transactions (Prohibition) Act, 2017.",
        )]

    if ratio < cfg.undervaluation_amber_pct:
        return [Finding(
            rule_id="R2",
            rule_name="Undervaluation vs DC rate",
            severity=Severity.AMBER,
            title=f"Declared price is {ratio}% of DC value",
            detail=f"Declared Rs {ctx.declared_price_pkr:,} vs DC value Rs {dc_value:,}.",
            evidence=evidence,
            recommended_action="Ask the parties to justify the valuation; note in the mutation order.",
        )]
    return []


def rule_r3_seller_viability(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R3 — Seller is dead (per NADRA) or implausibly old.

    Transferring land out of a dead person's name is the most common single act
    of land fraud in Pakistan. Age is the weaker half of this rule: KP's
    NADRA-linked age data is imperfect, so extreme age raises RED but the
    *decision layer* routes it to enhanced verification rather than a hard
    block in Phase 1.
    """
    findings: list[Finding] = []

    for i, seller in enumerate(ctx.sellers):
        base = {
            "party_index": i,
            "name": seller.name,
            "cnic_masked": mask_cnic(seller.cnic),
            "age": seller.age,
            "nadra_status": seller.nadra_status.value,
            "biometric_verified": seller.biometric_verified,
        }

        if seller.nadra_status is NadraStatus.DECEASED:
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.RED,
                title=f"Seller '{seller.name}' is recorded deceased by NADRA",
                detail=("NADRA records this CNIC as deceased. A deceased person "
                        "cannot execute a transfer. This mutation should be "
                        "reprocessed as an inheritance mutation with all legal "
                        "heirs, or rejected."),
                evidence={**base, "check": "NADRA_DEATH_REGISTER"},
                recommended_action=("BLOCK. If the transfer is legitimate it must "
                                    "proceed as an inheritance mutation with the "
                                    "full list of legal heirs and a succession "
                                    "certificate."),
                legal_basis="KP Land Revenue Act — inheritance mutation (Intiqal-e-Wirasat).",
            ))
            continue

        if seller.nadra_status is NadraStatus.BLOCKED:
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.RED,
                title=f"Seller CNIC is blocked / cancelled ({mask_cnic(seller.cnic)})",
                detail="A blocked CNIC cannot be used to execute a transfer.",
                evidence={**base, "check": "NADRA_CNIC_STATUS"},
                recommended_action="BLOCK. Require a valid, live CNIC.",
            ))
            continue

        if seller.nadra_status is NadraStatus.UNVERIFIED:
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.AMBER,
                title="Seller identity was not checked against NADRA",
                detail=("No NADRA verification result is on record for this CNIC. "
                        "This is usually a connectivity or integration gap rather "
                        "than fraud, but a transfer should not be attested on an "
                        "identity nobody has checked."),
                evidence={**base, "check": "NADRA_UNVERIFIED"},
                recommended_action="Complete a NADRA biometric check at e-Sahulat.",
            ))
            continue

        if seller.nadra_status is NadraStatus.NOT_FOUND:
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.AMBER,
                title=f"Seller CNIC not found in NADRA ({mask_cnic(seller.cnic)})",
                detail=("The CNIC could not be resolved. This is common for older "
                        "records and for non-digitised mouzas, but it is also how "
                        "fabricated sellers appear."),
                evidence={**base, "check": "NADRA_LOOKUP"},
                recommended_action="Resolve identity at a NADRA e-Sahulat centre before attestation.",
            ))
            continue

        if seller.age is not None and seller.age > cfg.seller_age_red:
            sev = Severity.RED if not seller.biometric_verified else Severity.AMBER
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=sev,
                title=f"Seller age {seller.age} exceeds {cfg.seller_age_red}",
                detail=(f"The seller is recorded as {seller.age} years old. "
                        + ("No live biometric was captured. " if not seller.biometric_verified else
                           "A live biometric was captured. ")
                        + "Extreme-age sellers are a known vector: the transfer is "
                          "often executed after death, or under coercion, using an "
                          "identity that has not been re-verified in years."),
                evidence={**base, "threshold": cfg.seller_age_red},
                recommended_action=("Require live NADRA biometric plus a recorded "
                                    "video attestation in the presence of the "
                                    "Tehsildar or two gazetted witnesses."),
            ))
        elif seller.age is not None and seller.age > cfg.seller_age_amber:
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.AMBER,
                title=f"Seller age {seller.age} above watch threshold {cfg.seller_age_amber}",
                detail="Elderly seller — enhanced identity verification recommended.",
                evidence={**base, "threshold": cfg.seller_age_amber},
                recommended_action="Live biometric verification and a witness attestation.",
            ))

        if seller.cnic and not valid_cnic(seller.cnic):
            findings.append(Finding(
                rule_id="R3",
                rule_name="Seller not viable",
                severity=Severity.AMBER,
                title="Malformed seller CNIC",
                detail=f"CNIC '{mask_cnic(seller.cnic)}' is not a valid 13-digit NADRA number.",
                evidence={**base},
                recommended_action="Correct the CNIC on record.",
            ))

    return findings


def rule_r4_power_of_attorney(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R4 — Power of attorney without biometric verification.

    A POA lets one person dispose of another's land. Without a biometric check
    on the attorney (and, ideally, on the principal at the time the POA was
    executed), a POA is a blank cheque — and it is the instrument of choice for
    grabbing the land of overseas Pakistanis and widows.
    """
    findings: list[Finding] = []

    for i, party in enumerate(tuple(ctx.sellers) + tuple(ctx.buyers)):
        if not party.acting_via_poa:
            continue
        base = {
            "party_index": i,
            "name": party.name,
            "role": "seller" if i < len(ctx.sellers) else "buyer",
            "attorney_cnic_masked": mask_cnic(party.poa_attorney_cnic),
            "poa_registered": party.poa_registered,
            "poa_biometric_verified": party.poa_biometric_verified,
        }

        if cfg.poa_requires_biometric and not party.poa_biometric_verified:
            findings.append(Finding(
                rule_id="R4",
                rule_name="Unverified power of attorney",
                severity=Severity.RED,
                title=f"{base['role'].title()} acting via POA without biometric verification",
                detail=("A power of attorney is being used to dispose of or acquire "
                        "land, and the attorney's identity has not been confirmed "
                        "biometrically through NADRA. An unverified POA is the "
                        "highest-risk instrument in land transfer: it separates the "
                        "person signing from the person who owns the land."),
                evidence=base,
                recommended_action=("BLOCK until the attorney verifies biometrically "
                                    "at a NADRA e-Sahulat centre and the POA is "
                                    "confirmed against the registered deed."),
                legal_basis="Power of Attorney Act, 1882; Registration Act, 1908 s.17/18.",
            ))

        if not party.poa_registered:
            findings.append(Finding(
                rule_id="R4",
                rule_name="Unverified power of attorney",
                severity=Severity.AMBER,
                title="POA is not registered",
                detail="The power of attorney is not on record as a registered instrument.",
                evidence=base,
                recommended_action="Produce the registered POA deed and its registration number.",
            ))

        if party.poa_attorney_cnic and not valid_cnic(party.poa_attorney_cnic):
            findings.append(Finding(
                rule_id="R4",
                rule_name="Unverified power of attorney",
                severity=Severity.AMBER,
                title="Malformed attorney CNIC",
                detail="The attorney's CNIC is not a valid 13-digit NADRA number.",
                evidence=base,
                recommended_action="Correct the attorney CNIC and re-verify.",
            ))

    return findings


def rule_r5_identity_drift(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R5 — Caste / mouza mismatch against the old manual ``latha``.

    The pre-computerisation record is our only independent witness. Before
    LRMIS, identity in a Jamabandi was established by name + father's name +
    grandfather's name, and by the ``patti``/``taraf`` — the village quarter,
    which in KP is very often named after the caste or clan that lives there.
    A seller whose recorded ``qaum`` or mouza does not match the latha is
    either a data-entry artefact (common) or a different person entirely
    (occasionally catastrophic).

    **This rule never returns RED on its own.** Manual KP records contain heavy
    transliteration noise, clan names change, and women routinely move mouza on
    marriage. Blocking a legitimate inheritance on a spelling difference is a
    worse failure than referring a doubtful case to a human.
    """
    latha = ctx.latha
    if latha is None:
        return [Finding(
            rule_id="R5",
            rule_name="Latha identity drift",
            severity=Severity.INFO,
            title="No old latha record linked",
            detail=("No pre-computerisation record is linked to this khasra, so "
                    "identity cannot be cross-checked. Scanning and linking the "
                    "manual latha is the single highest-value data task for this "
                    "district."),
            evidence={"mouza": ctx.mouza_code, "khasra_no": ctx.khasra_no},
            recommended_action="Link the scanned latha page for this khasra.",
        )]

    findings: list[Finding] = []
    issues: dict[str, Any] = {"latha_owner": latha.owner_name,
                              "latha_mouza": latha.mouza,
                              "latha_qaum": latha.qaum,
                              "jamabandi_year": latha.jamabandi_year}

    # --- Caste / qaum mismatch (advisory only) ---
    buyer_qaums = {p.qaum for p in ctx.buyers if p.qaum}
    seller_qaums = {p.qaum for p in ctx.sellers if p.qaum}
    qaum_mismatch = False
    if latha.qaum:
        norm_latha = normalise_name(latha.qaum)
        all_qaums = {normalise_name(q) for q in (buyer_qaums | seller_qaums) if q}
        if all_qaums and norm_latha and norm_latha not in all_qaums:
            qaum_mismatch = True
            issues["qaum_latha"] = latha.qaum
            issues["qaum_parties"] = sorted(buyer_qaums | seller_qaums)

    # --- Mouza mismatch (advisory only) ---
    mouza_mismatch = bool(
        latha.mouza and ctx.mouza_name
        and normalise_name(latha.mouza) != normalise_name(ctx.mouza_name))
    if mouza_mismatch:
        issues["mouza_current"] = ctx.mouza_name

    # --- Name match against latha ---
    best = Decimal("0")
    for party in tuple(ctx.sellers):
        score = name_similarity(party.name, latha.owner_name)
        if latha.father_name and party.father_name:
            score = (score + name_similarity(party.father_name, latha.father_name)) / 2
        best = max(best, score)
    issues["name_similarity"] = str(best)

    comparable = bool(normalise_name(latha.owner_name)
                      and any(normalise_name(p.name) for p in ctx.sellers))
    mismatch_parts = []
    if qaum_mismatch:
        mismatch_parts.append("caste/clan")
    if mouza_mismatch:
        mismatch_parts.append("mouza")

    severity = None
    if comparable and best < cfg.latha_match_min_score:
        # The name is the strong signal. A name with no overlap at all is the
        # loudest case, not a reason to stay quiet.
        severity = Severity.AMBER
        extra = (f" The recorded caste/clan and mouza also differ "
                 f"({', '.join(mismatch_parts)})." if mismatch_parts else "")
        title = ("Seller name does not match the old latha"
                 + (" (caste/mouza also differ)" if mismatch_parts else ""))
        detail = (f"The manual register records the owner as '{latha.owner_name}'"
                  + (f", qaum {latha.qaum}" if latha.qaum else "")
                  + (f", in mouza '{latha.mouza}'" if latha.mouza else "")
                  + f". The seller on this mutation matches at {best} "
                    f"(threshold {cfg.latha_match_min_score})." + extra
                  + " This is frequently a transliteration difference, or a "
                    "record that was never mutated after the previous owner died "
                    "— but it must be reconciled on the record, not ignored.")
    elif qaum_mismatch or mouza_mismatch:
        severity = Severity.INFO
        title = "Caste or mouza differs from the old latha"
        detail = ("The recorded caste/mouza differs from the manual register. "
                  "Recorded for pattern analysis only; caste and mouza are weak "
                  "identifiers and routinely change legitimately (marriage, "
                  "migration, clan renaming).")

    if severity is not None:
        findings.append(Finding(
            rule_id="R5",
            rule_name="Latha identity drift",
            severity=severity,
            title=title,
            detail=detail,
            evidence={**issues,
                      "severity_cap": "AMBER — R5 cannot block alone by design "
                                      "because manual KP records are noisy."},
            recommended_action=("Reconcile against the scanned latha page"
                                + (f" ({latha.scanned_page})" if latha.scanned_page else "")
                                + " and record a Fard Badar / correction entry with "
                                  "the reason for the difference."),
            legal_basis="KP Land Revenue Act — correction of record (Fard Badar).",
        ))

    # --- Area drift ---
    if latha.area_kanal and ctx.area_kanal:
        drift = abs(ctx.area_kanal - latha.area_kanal)
        pct = _pct(drift, latha.area_kanal)
        if pct > cfg.latha_area_tolerance_pct:
            findings.append(Finding(
                rule_id="R5",
                rule_name="Latha identity drift",
                severity=Severity.AMBER,
                title=f"Area differs from latha by {pct}%",
                detail=(f"The manual record shows {latha.area_kanal} kanal; this "
                        f"mutation uses {ctx.area_kanal} kanal. Area inflation is "
                        "how a seller conveys more land than they own."),
                evidence={"latha_area_kanal": str(latha.area_kanal),
                          "mutation_area_kanal": str(ctx.area_kanal),
                          "drift_pct": str(pct),
                          "tolerance_pct": str(cfg.latha_area_tolerance_pct)},
                recommended_action="Order a fresh measurement (Taqseem/Mustatil) before attestation.",
            ))

    return findings


def rule_r6_geo_fence(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R6 (extended) — the Patwari's GPS fix is not on the land.

    Closes the 'desktop mutation' loophole: an entry can only be attested from
    a device that was physically standing on the parcel (see
    core/ground_trust.py for how we make that hard to fake).
    """
    g = ctx.ground
    if g is None or not g.verified:
        if ctx.transaction_type.value in cfg.ground_required_for:
            return [Finding(
                rule_id="R6",
                rule_name="Ground verification missing",
                severity=Severity.RED,
                title="No on-site ground verification for this transaction type",
                detail=(f"A {ctx.transaction_type.value} requires the Patwari to "
                        "attend the site and capture GPS + photo + two witness "
                        "CNICs. No such record exists. Without it there is no "
                        "evidence anyone checked that the seller possesses what "
                        "they are selling."),
                evidence={"transaction_type": ctx.transaction_type.value,
                          "mouza": ctx.mouza_code, "khasra_no": ctx.khasra_no},
                recommended_action="Complete ground verification via the Patwari app.",
            )]
        return []

    findings: list[Finding] = []
    if g.mock_location_suspected:
        findings.append(Finding(
            rule_id="R6",
            rule_name="Ground verification missing",
            severity=Severity.RED,
            title="Mock / spoofed GPS location suspected",
            detail=("The device reported a location that fails anti-spoof checks "
                    "(mock provider, impossible travel speed, or a fix inconsistent "
                    "with satellite data). The Patwari may not have been on site."),
            evidence={"device_id": g.device_id,
                      "accuracy_m": g.accuracy_m,
                      "captured_at": g.captured_at.isoformat() if g.captured_at else None},
            recommended_action="BLOCK. Re-verify in person; investigate the device.",
        ))

    if g.inside_mouza is False:
        dist = g.distance_to_boundary_m
        if dist is None or dist > cfg.geo_fence_max_distance_m:
            findings.append(Finding(
                rule_id="R6",
                rule_name="Ground verification missing",
                severity=Severity.RED,
                title="Verification captured outside the mouza boundary",
                detail=(f"The GPS fix falls outside the digitised boundary of "
                        f"{ctx.mouza_name}"
                        + (f" by about {dist:.0f} m" if dist is not None else "")
                        + ". Either the parcel boundary data is wrong or the "
                          "verification was not performed on this land."),
                evidence={"distance_to_boundary_m": dist,
                          "tolerance_m": cfg.geo_fence_max_distance_m,
                          "lat": g.latitude, "lon": g.longitude},
                recommended_action=("BLOCK. If the boundary data is at fault, raise "
                                    "a GIS correction ticket — do not attest against "
                                    "a bad polygon."),
            ))

    if cfg.witness_required and len(g.witness_cnics) < cfg.witness_required:
        findings.append(Finding(
            rule_id="R6",
            rule_name="Ground verification missing",
            severity=Severity.AMBER,
            title=f"Only {len(g.witness_cnics)} of {cfg.witness_required} witness CNICs captured",
            detail="Community witness attestation is incomplete.",
            evidence={"witnesses": len(g.witness_cnics), "required": cfg.witness_required},
            recommended_action="Capture two neighbouring landowners' CNICs on site.",
        ))

    if g.synced_at and g.captured_at:
        lag_h = (g.synced_at - g.captured_at).total_seconds() / 3600
        if lag_h > cfg.offline_sync_red_hours:
            findings.append(Finding(
                rule_id="R6",
                rule_name="Ground verification missing",
                severity=Severity.RED,
                title=f"Ground evidence synced {lag_h:.0f}h after capture",
                detail=("The app was offline for an unusually long period. Long "
                        "offline windows are legitimate in remote KP, but they are "
                        "also when evidence gets staged. Treat as unverified."),
                evidence={"offline_hours": round(lag_h, 1),
                          "red_threshold_hours": cfg.offline_sync_red_hours},
                recommended_action="Re-verify on site.",
            ))
        elif lag_h > cfg.offline_sync_warn_hours:
            findings.append(Finding(
                rule_id="R6",
                rule_name="Ground verification missing",
                severity=Severity.AMBER,
                title=f"Ground evidence synced {lag_h:.0f}h after capture",
                detail="Delayed sync — corroborate with the photo EXIF timestamp.",
                evidence={"offline_hours": round(lag_h, 1),
                          "amber_threshold_hours": cfg.offline_sync_warn_hours},
                recommended_action="Cross-check EXIF timestamp; note the delay in the order.",
            ))
    return findings


def rule_r7_double_sale(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R7 (extended) — ownership chain / double-sale check.

    The Punjab Assembly's own debate on land records produced the line 'multiple
    people had sometimes been shown as owners of the same two-acre plot'. That is
    exactly what this rule catches: conveying a share the seller no longer holds,
    or pushing a khasra's total recorded shares past 100%.
    """
    findings: list[Finding] = []

    # Oversubscription is checked independently of whether we know the seller's
    # own holding: if a khasra's shares already sum to more than a whole, the
    # record is internally inconsistent whoever is selling.
    if ctx.khasra_shares_total is not None and ctx.khasra_shares_total > Decimal("1"):
        findings.append(Finding(
            rule_id="R7",
            rule_name="Double sale / oversold share",
            severity=Severity.RED,
            title=f"Khasra shares total {ctx.khasra_shares_total} (> 100%)",
            detail=("The sum of all recorded ownership shares in this khasra "
                    "exceeds a whole. The record is internally inconsistent — "
                    "someone's share has been double-counted, or two owners are "
                    "each recorded as holding the same land."),
            evidence={"khasra_shares_total": str(ctx.khasra_shares_total),
                      "khasra_no": ctx.khasra_no},
            recommended_action=("Freeze new mutations on this khasra pending a "
                                "share audit by the Tehsildar."),
        ))

    if ctx.seller_held_share is None:
        # We do not know what the seller holds, because the share register for
        # this khasra has never been populated. That is a data gap, not
        # evidence of fraud — and a rule that shouts "DOUBLE SALE" at every
        # unpopulated khasra is a rule that gets switched off within a month.
        findings.append(Finding(
            rule_id="R7",
            rule_name="Double sale / oversold share",
            severity=Severity.INFO,
            title="Ownership shares not yet recorded for this khasra",
            detail=("No share register exists for this khasra, so the seller's "
                    "holding cannot be checked. Populating the share register "
                    "from the jamabandi is a prerequisite for double-sale "
                    "detection in this mouza."),
            evidence={"khasra_no": ctx.khasra_no, "mouza": ctx.mouza_code,
                      "check": "SKIPPED_NO_DATA"},
            recommended_action="Load the khasra share register before enforcing R7 here.",
        ))
    else:
        sold = sum((s.share() for s in ctx.sellers if s.share() is not None), Decimal("0"))
        if sold <= 0:
            sold = Decimal("1")  # full-share sale when shares are unrecorded
        if sold > ctx.seller_held_share:
            findings.append(Finding(
                rule_id="R7",
                rule_name="Double sale / oversold share",
                severity=Severity.RED,
                title="Seller is conveying more than they hold",
                detail=(f"The seller's recorded holding is "
                        f"{ctx.seller_held_share} of this khasra, but the mutation "
                        f"conveys {sold}. Either the seller has already sold part of "
                        "it, or the share on record is wrong. Both must be resolved "
                        "before attestation or the same land ends up sold twice."),
                evidence={"seller_held_share": str(ctx.seller_held_share),
                          "conveyed_share": str(sold),
                          "khasra_no": ctx.khasra_no, "mouza": ctx.mouza_code},
                recommended_action=("BLOCK. Reconcile the ownership chain; if the "
                                    "land was genuinely sold twice, the second buyer "
                                    "has a civil remedy and the case goes to the "
                                    "Tehsildar/Collector."),
            ))

    # Buyer-side ring detection: one buyer sweeping many khasras quickly.
    buyer_cnics = [b.cnic for b in ctx.buyers
                   if b.cnic and b.cnic in {s.cnic for s in ctx.sellers if s.cnic}]
    if buyer_cnics:
        findings.append(Finding(
            rule_id="R7",
            rule_name="Double sale / oversold share",
            severity=Severity.AMBER,
            title="A party appears as both buyer and seller",
            detail="Self-dealing / round-tripping detected — a common way to "
                   "manufacture a chain of title.",
            evidence={"cnics_masked": [mask_cnic(c) for c in buyer_cnics]},
            recommended_action="Verify the consideration and the parties' relationship.",
        ))
    return findings


def rule_r8_patwari_velocity(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R8 (extended) — officer-level anomaly.

    Not an accusation: a high daily count is often just a backlog being cleared.
    It is recorded because sustained anomaly at one Patwari circle is how
    organised (rather than opportunistic) fraud shows up.
    """
    if ctx.patwari_id and ctx.patwari_mutations_today > cfg.patwari_daily_mutation_amber:
        return [Finding(
            rule_id="R8",
            rule_name="Officer velocity anomaly",
            severity=Severity.AMBER,
            title=f"Patwari has processed {ctx.patwari_mutations_today} mutations today",
            detail=("Above the review threshold of "
                    f"{cfg.patwari_daily_mutation_amber}. This may be legitimate "
                    "backlog clearance, but sustained volume at one circle is a "
                    "known indicator of organised mutation fraud."),
            evidence={"patwari_id": ctx.patwari_id,
                      "mutations_today": ctx.patwari_mutations_today,
                      "threshold": cfg.patwari_daily_mutation_amber},
            recommended_action="Sample-audit 10% of this officer's attestations this week.",
        )]
    return []


def rule_r9_missing_identity(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]:
    """R9 (extended) — the boring data-quality rules that stop everything else."""
    findings: list[Finding] = []
    if not any(valid_cnic(p.cnic) for p in ctx.buyers):
        findings.append(Finding(
            rule_id="R9",
            rule_name="Incomplete identity data",
            severity=Severity.AMBER,
            title="No valid buyer CNIC on the mutation",
            detail=("Every party to a mutation must carry a valid 13-digit CNIC. "
                    "Without it, the record cannot be linked to NADRA, cannot be "
                    "checked for death, and cannot be traced later."),
            evidence={"buyers": [mask_cnic(p.cnic) for p in ctx.buyers]},
            recommended_action="Capture and verify buyer CNICs before attestation.",
        ))
    if ctx.area_kanal <= 0:
        findings.append(Finding(
            rule_id="R9",
            rule_name="Incomplete identity data",
            severity=Severity.AMBER,
            title="Mutation has no positive area",
            detail="Area is zero or missing; valuation and share checks are impossible.",
            evidence={"area_kanal": str(ctx.area_kanal)},
            recommended_action="Record the area from the latha / measurement.",
        ))
    unverified = [p.name for p in tuple(ctx.sellers) + tuple(ctx.buyers)
                  if not p.biometric_verified]
    if unverified and ctx.transaction_type is TransactionType.SALE:
        findings.append(Finding(
            rule_id="R9",
            rule_name="Incomplete identity data",
            severity=Severity.AMBER,
            title=f"{len(unverified)} party/parties not biometrically verified",
            detail="NADRA biometric verification is mandatory for all parties to a sale.",
            evidence={"unverified_parties": unverified},
            recommended_action="Complete biometric verification at NADRA e-Sahulat.",
        ))
    return findings


# --------------------------------------------------------------------------
# Registry & evaluation
# --------------------------------------------------------------------------

RuleFn = Callable[[MutationContext, RuleConfig], list[Finding]]

RULE_REGISTRY: dict[str, RuleFn] = {
    "R1": rule_r1_rapid_remutation,
    "R2": rule_r2_undervaluation,
    "R3": rule_r3_seller_viability,
    "R4": rule_r4_power_of_attorney,
    "R5": rule_r5_identity_drift,
    "R6": rule_r6_geo_fence,
    "R7": rule_r7_double_sale,
    "R8": rule_r8_patwari_velocity,
    "R9": rule_r9_missing_identity,
}


def evaluate(
    ctx: MutationContext,
    config: RuleConfig = DEFAULT_CONFIG,
    *,
    rules: Sequence[str] | None = None,
) -> EvaluationResult:
    """Run the rule set and return a scored result.

    Score is a transparent weighted count, clamped to 0..100. It is *not* a
    probability and must never be presented as one.
    """
    selected = rules or sorted(RULE_REGISTRY, key=lambda r: int(r[1:]))
    findings: list[Finding] = []
    for rule_id in selected:
        fn = RULE_REGISTRY.get(rule_id)
        if fn is None:
            raise KeyError(f"unknown rule {rule_id!r}; known: {sorted(RULE_REGISTRY)}")
        findings.extend(fn(ctx, config))

    red = sum(1 for f in findings if f.severity is Severity.RED)
    amber = sum(1 for f in findings if f.severity is Severity.AMBER)
    info = sum(1 for f in findings if f.severity is Severity.INFO)

    raw = red * config.weight_red + amber * config.weight_amber + info * config.weight_info
    score = max(0, min(100, raw))

    order = {Severity.RED: 0, Severity.AMBER: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: (order[f.severity], f.rule_id))

    return EvaluationResult(
        mutation_id=ctx.mutation_id,
        findings=findings,
        score=score,
        red_count=red,
        amber_count=amber,
        config_fingerprint=config.fingerprint(),
    )
