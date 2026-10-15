"""Pydantic request/response models for the public API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


# --------------------------------------------------------------------------
# Clearance (the e-Fard integration surface)
# --------------------------------------------------------------------------

class ClearanceRequest(BaseModel):
    """What e-Fard sends us before it issues a Fard or attests a mutation."""
    mutation_id: str = Field(..., examples=["MUT-KP-1001"])
    idempotency_key: str | None = None
    efard_ref: str | None = None
    channel: Literal["PORTAL", "E_SAHULAT", "DASTAK", "SDC"] = "PORTAL"

    # Optional: e-Fard may push the record instead of us pulling it.
    payload: dict[str, Any] | None = None


class ClearanceResponseModel(Orm):
    clearance_id: str
    mutation_id: str | None
    decision: str
    phase: str
    binding: bool
    severity: str
    score: int
    reasons: list[str]
    required_actions: list[dict[str, Any]]
    escalation_role: str | None
    expires_at: datetime | None
    ledger_seq: int | None
    ledger_hash: str | None
    config_fingerprint: str | None
    decided_at: datetime
    verify_url: str | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)


class MirrorRequest(BaseModel):
    """What e-Fard tells us it already issued (Mode B, retrospective audit)."""
    fard_id: str
    mutation_id: str | None = None
    district_code: str
    mouza_code: str
    khasra_no: str
    issued_at: datetime | None = None
    qr_payload: str | None = None
    document_sha256: str | None = None
    channel: Literal["PORTAL", "E_SAHULAT", "DASTAK", "SDC"] = "PORTAL"


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

class LedgerEntryOut(Orm):
    seq: int
    chain_key: str
    prev_hash: str
    entry_hash: str
    event_type: str
    entity_type: str
    entity_id: str
    actor_id: str
    actor_role: str
    district_code: str | None
    payload: dict[str, Any]
    occurred_at: datetime
    created_at: datetime


class TamperFindingOut(BaseModel):
    kind: str
    seq: int | None
    detail: str
    expected: str | None
    found: str | None


class ChainVerificationOut(BaseModel):
    chain_key: str
    entries_checked: int
    intact: bool
    first_seq: int | None
    last_seq: int | None
    head_hash: str | None
    recomputed_root: str | None
    checked_at: datetime
    findings: list[TamperFindingOut]


class AnchorOut(Orm):
    id: int
    chain_key: str
    from_seq: int
    to_seq: int
    entry_count: int
    merkle_root: str
    signature: str | None
    signed_by: str | None
    anchored_at: datetime
    published_ref: str | None
    published_at: datetime | None
    # Computed at read time, not stored: recomputing is the point. An anchor
    # whose stored signature no longer verifies is the exact event we exist to
    # surface, so it must not be denormalised into a column someone can edit.
    signature_valid: bool | None = None


class AnchorCreate(BaseModel):
    published_ref: str | None = Field(
        default=None,
        description="Where the Merkle root was published (Gazette ref, URL, newspaper).")


# --------------------------------------------------------------------------
# Fraud flags
# --------------------------------------------------------------------------

class FraudFlagOut(Orm):
    id: int
    mutation_id: str | None
    district_code: str
    mouza_code: str | None
    khasra_no: str | None
    rule_id: str
    rule_name: str
    severity: str
    title: str
    detail: str
    evidence: dict[str, Any]
    recommended_action: str | None
    legal_basis: str | None
    status: str
    engine_version: str | None
    raised_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None


class FlagReview(BaseModel):
    status: Literal["OPEN", "UNDER_REVIEW", "CONFIRMED_FRAUD", "DISMISSED", "REFERRED"]
    note: str = Field(..., min_length=10,
                      description="A written reason is mandatory — a dismissed flag "
                                  "without one is indistinguishable from an override.")


# --------------------------------------------------------------------------
# Ground verification (Patwari PWA)
# --------------------------------------------------------------------------

class GroundSubmissionIn(BaseModel):
    """One site visit from the PWA. Everything is a claim until scored."""
    client_submission_id: str | None = None
    mutation_id: str | None = None
    district_code: str | None = None
    mouza_code: str | None = None
    khasra_no: str | None = None

    officer_id: str | None = None
    device_id: str | None = None
    app_version: str | None = None

    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy_m: float | None = None
    altitude_m: float | None = None
    provider: str | None = None
    is_mock: bool | None = None
    satellites: int | None = None
    monotonic_ms: int | None = None
    captured_at: datetime | None = None

    photo_sha256: str | None = None
    photo_phash: str | None = None
    photo_bytes: int | None = None
    photo_exif_datetime: datetime | None = None
    photo_source: Literal["camera", "gallery", "unknown"] = "camera"

    witness_cnics: list[str] = Field(default_factory=list, max_length=6)
    notes: str | None = None

    nonce: str | None = None
    hmac: str | None = None

    @field_validator("photo_source")
    @classmethod
    def _no_gallery(cls, v: str) -> str:
        # A gallery upload proves nothing about presence. We accept the field so
        # we can record the attempt, but scoring rejects it.
        return v


class TrustSignalOut(BaseModel):
    code: str
    passed: bool
    detail: str
    penalty: int
    value: Any = None


class GroundVerificationOut(Orm):
    id: str
    mutation_id: str | None
    district_code: str
    mouza_code: str
    khasra_no: str | None
    officer_id: str | None
    device_id: str | None
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None
    inside_mouza: bool | None
    distance_to_boundary_m: float | None
    photo_sha256: str | None
    photo_source: str | None
    trust_score: int | None
    trust_band: str | None
    trust_signals: list[dict[str, Any]]
    status: str
    captured_at: datetime | None
    received_at: datetime
    offline_hours: float | None
    witness_cnic_hashes: list[str]


class DeviceEnrolIn(BaseModel):
    device_id: str
    officer_id: str
    label: str | None = None
    platform: str | None = None
    app_version: str | None = None


class DeviceEnrolOut(BaseModel):
    device_id: str
    officer_id: str
    attestation_secret: str
    device_token: str
    note: str = ("Store the attestation secret in the device keystore. It is shown "
                 "once and cannot be retrieved.")


# --------------------------------------------------------------------------
# Mutations & ownership
# --------------------------------------------------------------------------

class MutationOut(Orm):
    id: str
    efard_ref: str | None
    district_code: str
    tehsil_code: str
    mouza_code: str
    mouza_name: str | None
    khewat_no: str | None
    khasra_no: str
    area_kanal: Decimal
    transaction_type: str
    declared_price_pkr: Decimal
    dc_value_pkr: Decimal | None
    patwari_id: str | None
    status: str
    proposed_at: datetime
    decided_at: datetime | None


class ShareOut(BaseModel):
    khasra_key: str
    total_share: str
    oversubscribed: bool
    owners: list[dict[str, Any]]


# --------------------------------------------------------------------------
# USSD
# --------------------------------------------------------------------------

class UssdIn(BaseModel):
    sessionId: str
    msisdn: str = ""
    text: str = ""
    serviceCode: str | None = None


class UssdOut(BaseModel):
    message: str
    continueSession: bool


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

class DashboardOut(BaseModel):
    phase: str
    pilot_districts: list[str]
    chain: dict[str, Any]
    flags_by_rule: dict[str, int]
    flags_by_severity: dict[str, int]
    ground: dict[str, Any]
    mutations_by_status: dict[str, int]
    oversubscribed_khasras: list[dict[str, Any]]
    geo_engine: str
    generated_at: datetime
