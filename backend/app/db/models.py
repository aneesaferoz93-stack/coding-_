"""
ORM models.

Portability note
----------------
Production is PostgreSQL + PostGIS. Geometry is stored as WKT in ``*_wkt``
columns that exist on every dialect, and PostgreSQL additionally maintains real
``geometry`` columns as ``GENERATED ALWAYS AS (ST_GeomFromText(...)) STORED``
(see db/migrations). That gives us true spatial indexing, ST_Contains and
ST_Distance in production, while keeping the whole system runnable on a SQLite
file for pilots, demos and CI. ``app/db/geo.py`` picks the right code path and
always reports which one it used — a silent fallback is how geofencing bugs
become fraud.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSONB().with_variant(JSON(), "sqlite")

# SQLite only auto-assigns a rowid for a column literally declared INTEGER
# PRIMARY KEY; SQLAlchemy renders BigInteger as BIGINT, which SQLite will not
# auto-fill. Without this variant every INSERT into audit_log fails with
# "NOT NULL constraint failed: audit_log.id" on SQLite.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonType}


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class MutationStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    CLEARED = "CLEARED"
    HELD = "HELD"
    BLOCKED = "BLOCKED"
    ATTESTED = "ATTESTED"
    REJECTED = "REJECTED"
    OVERRIDDEN = "OVERRIDDEN"   # officer proceeded despite a flag — always recorded


class VerificationStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class FlagStatus(str, enum.Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD"
    DISMISSED = "DISMISSED"      # reviewed and found legitimate — must state why
    REFERRED = "REFERRED"        # handed to anti-corruption / police / courts


class OfficerRole(str, enum.Enum):
    PATWARI = "PATWARI"
    QANOONGO = "QANOONGO"
    TEHSILDAR = "TEHSILDAR"
    NAIB_TEHSILDAR = "NAIB_TEHSILDAR"
    DEPUTY_COMMISSIONER = "DEPUTY_COMMISSIONER"
    BOARD_OF_REVENUE = "BOARD_OF_REVENUE"
    AUDITOR = "AUDITOR"
    SYSTEM = "SYSTEM"


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

class District(Base):
    __tablename__ = "district"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(128))
    name_ur: Mapped[str | None] = mapped_column(String(128))
    name_ps: Mapped[str | None] = mapped_column(String(128))
    phase: Mapped[str] = mapped_column(String(24), default="PHASE_1_DUAL_RUN")
    live: Mapped[bool] = mapped_column(Boolean, default=True)
    e_registry_live: Mapped[bool] = mapped_column(Boolean, default=False)


class Mouza(Base):
    """Revenue estate. ``boundary_wkt`` is the digitised cadastral outline."""

    __tablename__ = "mouza"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    district_code: Mapped[str] = mapped_column(ForeignKey("district.code"))
    tehsil_code: Mapped[str] = mapped_column(String(32))
    name_en: Mapped[str] = mapped_column(String(160))
    name_ur: Mapped[str | None] = mapped_column(String(160))
    name_ps: Mapped[str | None] = mapped_column(String(160))
    boundary_wkt: Mapped[str | None] = mapped_column(Text)
    centroid_lat: Mapped[float | None] = mapped_column()
    centroid_lon: Mapped[float | None] = mapped_column()
    digitised: Mapped[bool] = mapped_column(Boolean, default=False)
    latha_scanned_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    total_khasras: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_mouza_district", "district_code"),)


class DcRate(Base):
    """Deputy Commissioner's valuation table — the reference for Rule 2."""

    __tablename__ = "dc_rate"
    id: Mapped[int] = mapped_column(primary_key=True)
    district_code: Mapped[str] = mapped_column(String(16))
    mouza_code: Mapped[str | None] = mapped_column(String(32))
    tehsil_code: Mapped[str | None] = mapped_column(String(32))
    land_type: Mapped[str] = mapped_column(String(32), default="AGRICULTURAL")
    rate_per_kanal: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_notification: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index("ix_dcrate_lookup", "district_code", "mouza_code", "land_type"),
    )


class LathaRecord(Base):
    """Scanned pre-computerisation register — our independent witness for Rule 5."""

    __tablename__ = "latha_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    district_code: Mapped[str] = mapped_column(String(16))
    mouza_code: Mapped[str] = mapped_column(String(32))
    khasra_no: Mapped[str] = mapped_column(String(32))
    khewat_no: Mapped[str | None] = mapped_column(String(32))
    owner_name: Mapped[str] = mapped_column(String(255))
    father_name: Mapped[str | None] = mapped_column(String(255))
    qaum: Mapped[str | None] = mapped_column(String(128))
    mouza_name_in_record: Mapped[str | None] = mapped_column(String(160))
    area_kanal: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    jamabandi_year: Mapped[int | None] = mapped_column(Integer)
    scanned_page: Mapped[str | None] = mapped_column(String(255))
    scan_sha256: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_latha_khasra", "district_code", "mouza_code", "khasra_no"),
    )


class Officer(Base):
    __tablename__ = "officer"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    role: Mapped[OfficerRole] = mapped_column(Enum(OfficerRole, native_enum=False))
    cnic_hash: Mapped[str | None] = mapped_column(String(64))
    district_code: Mapped[str | None] = mapped_column(String(16))
    tehsil_code: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FieldDevice(Base):
    """Handsets registered to a named officer. Ground evidence from an
    unregistered device is discounted (signal G11)."""

    __tablename__ = "field_device"
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    officer_id: Mapped[str | None] = mapped_column(ForeignKey("officer.id"))
    label: Mapped[str | None] = mapped_column(String(128))
    platform: Mapped[str | None] = mapped_column(String(32))
    app_version: Mapped[str | None] = mapped_column(String(32))
    attestation_secret: Mapped[str | None] = mapped_column(String(128))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------
# Core transaction tables
# --------------------------------------------------------------------------

class Mutation(Base):
    """A proposed or attested Intiqal. This is a *mirror* of the e-Fard/LRMIS
    record, never the system of record for title."""

    __tablename__ = "mutation"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    efard_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    district_code: Mapped[str] = mapped_column(String(16), index=True)
    tehsil_code: Mapped[str] = mapped_column(String(32))
    mouza_code: Mapped[str] = mapped_column(String(32), index=True)
    mouza_name: Mapped[str | None] = mapped_column(String(160))
    khewat_no: Mapped[str | None] = mapped_column(String(32))
    khasra_no: Mapped[str] = mapped_column(String(32), index=True)
    area_kanal: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    transaction_type: Mapped[str] = mapped_column(String(32), default="SALE")
    declared_price_pkr: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    dc_value_pkr: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    patwari_id: Mapped[str | None] = mapped_column(String(64))
    patwari_mutations_today: Mapped[int] = mapped_column(Integer, default=0)
    seller_held_share: Mapped[Decimal | None] = mapped_column(Numeric(12, 8))
    status: Mapped[MutationStatus] = mapped_column(
        Enum(MutationStatus, native_enum=False), default=MutationStatus.DRAFT, index=True)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    flags: Mapped[list[FraudFlag]] = relationship(back_populates="mutation")

    __table_args__ = (Index("ix_mutation_khasra_time", "district_code", "mouza_code",
                            "khasra_no", "proposed_at"),)


class Clearance(Base):
    """The answer we give e-Fard when it asks 'may I issue?'."""

    __tablename__ = "clearance"
    clearance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mutation_id: Mapped[str | None] = mapped_column(ForeignKey("mutation.id"))
    decision: Mapped[str] = mapped_column(String(16), index=True)
    phase: Mapped[str] = mapped_column(String(24))
    binding: Mapped[bool] = mapped_column(Boolean, default=False)
    severity: Mapped[str] = mapped_column(String(8))
    score: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    required_actions: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    escalation_role: Mapped[str | None] = mapped_column(String(64))
    ledger_seq: Mapped[int | None] = mapped_column(BigInteger)
    ledger_hash: Mapped[str | None] = mapped_column(String(64))
    config_fingerprint: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overridden_by: Mapped[str | None] = mapped_column(String(64))
    override_reason: Mapped[str | None] = mapped_column(Text)


class LedgerEntryORM(Base):
    """APPEND-ONLY. Protected by triggers in db/migrations/002_append_only.sql.

    If you can UPDATE or DELETE a row in this table, the entire integrity
    guarantee of the system is void — so we make the database refuse, and the
    health endpoint checks that it does.
    """

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    seq: Mapped[int] = mapped_column(BigInteger, index=True)
    chain_key: Mapped[str] = mapped_column(String(64), default="kp-main", index=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    entity_type: Mapped[str] = mapped_column(String(48))
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(32), default="SYSTEM")
    district_code: Mapped[str | None] = mapped_column(String(16), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("chain_key", "seq", name="uq_audit_chain_seq"),
        CheckConstraint("length(entry_hash) = 64", name="ck_audit_hash_len"),
        CheckConstraint("length(prev_hash) = 64", name="ck_audit_prevhash_len"),
    )


class ChainAnchor(Base):
    """Periodic signed checkpoint. Publishing the Merkle root outside the
    database is what makes tail-truncation detectable."""

    __tablename__ = "chain_anchor"
    id: Mapped[int] = mapped_column(primary_key=True)
    chain_key: Mapped[str] = mapped_column(String(64), default="kp-main")
    from_seq: Mapped[int] = mapped_column(BigInteger)
    to_seq: Mapped[int] = mapped_column(BigInteger)
    entry_count: Mapped[int] = mapped_column(Integer)
    merkle_root: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(String(128))
    signed_by: Mapped[str | None] = mapped_column(String(128))
    anchored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_ref: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_anchor_chain", "chain_key", "to_seq"),)


class FraudFlag(Base):
    """One rule firing on one mutation. Persisted so districts can be compared
    and so a dismissed flag carries the officer's written reason."""

    __tablename__ = "fraud_flag"
    id: Mapped[int] = mapped_column(primary_key=True)
    mutation_id: Mapped[str | None] = mapped_column(ForeignKey("mutation.id"), index=True)
    district_code: Mapped[str] = mapped_column(String(16), index=True)
    mouza_code: Mapped[str | None] = mapped_column(String(32), index=True)
    khasra_no: Mapped[str | None] = mapped_column(String(32), index=True)
    rule_id: Mapped[str] = mapped_column(String(8), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(8), index=True)
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    legal_basis: Mapped[str | None] = mapped_column(Text)
    status: Mapped[FlagStatus] = mapped_column(
        Enum(FlagStatus, native_enum=False), default=FlagStatus.OPEN, index=True)
    engine_version: Mapped[str | None] = mapped_column(String(16))
    config_fingerprint: Mapped[str | None] = mapped_column(String(64))
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)

    mutation: Mapped[Mutation] = relationship(back_populates="flags")


class GroundVerification(Base):
    """One Patwari site visit, with everything needed to audit it later."""

    __tablename__ = "ground_verification"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mutation_id: Mapped[str | None] = mapped_column(ForeignKey("mutation.id"), index=True)
    khasra_key: Mapped[str | None] = mapped_column(String(128), index=True)
    district_code: Mapped[str] = mapped_column(String(16), index=True)
    mouza_code: Mapped[str] = mapped_column(String(32))
    khasra_no: Mapped[str | None] = mapped_column(String(32))

    officer_id: Mapped[str | None] = mapped_column(String(64))
    device_id: Mapped[str | None] = mapped_column(String(128))

    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()
    accuracy_m: Mapped[float | None] = mapped_column()
    altitude_m: Mapped[float | None] = mapped_column()
    provider: Mapped[str | None] = mapped_column(String(32))
    is_mock: Mapped[bool | None] = mapped_column(Boolean)
    satellites: Mapped[int | None] = mapped_column(Integer)
    geom_wkt: Mapped[str | None] = mapped_column(Text)

    inside_mouza: Mapped[bool | None] = mapped_column(Boolean)
    distance_to_boundary_m: Mapped[float | None] = mapped_column()

    photo_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    photo_phash: Mapped[str | None] = mapped_column(String(32))
    photo_bytes: Mapped[int | None] = mapped_column(Integer)
    photo_exif_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    photo_source: Mapped[str | None] = mapped_column(String(16))

    witness_cnic_hashes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    witness_nadra_ok: Mapped[list[bool]] = mapped_column(JsonType, default=list)

    trust_score: Mapped[int | None] = mapped_column(Integer, index=True)
    trust_band: Mapped[str | None] = mapped_column(String(16))
    trust_signals: Mapped[list[Any]] = mapped_column(JsonType, default=list)
    trust_config_version: Mapped[str | None] = mapped_column(String(16))

    status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, native_enum=False),
        default=VerificationStatus.PENDING, index=True)

    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    offline_hours: Mapped[float | None] = mapped_column()
    app_version: Mapped[str | None] = mapped_column(String(32))
    client_submission_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_ground_mouza_time", "mouza_code", "captured_at"),
    )


class KhasraShare(Base):
    """Current ownership shares for a khasra, derived from the ledger.

    Double-sale detection (Rule 7) is arithmetic on this table: the shares of
    all living owners of a khasra must sum to <= 1.
    """

    __tablename__ = "khasra_share"
    id: Mapped[int] = mapped_column(primary_key=True)
    khasra_key: Mapped[str] = mapped_column(String(160), index=True)
    district_code: Mapped[str] = mapped_column(String(16))
    mouza_code: Mapped[str] = mapped_column(String(32))
    khasra_no: Mapped[str] = mapped_column(String(32))
    owner_cnic_hash: Mapped[str] = mapped_column(String(64), index=True)
    owner_name: Mapped[str | None] = mapped_column(String(255))
    share_num: Mapped[int] = mapped_column(Integer, default=1)
    share_den: Mapped[int] = mapped_column(Integer, default=1)
    area_kanal: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_mutation_id: Mapped[str | None] = mapped_column(String(64))
    ledger_seq: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_share_current", "khasra_key", "valid_to"),
        CheckConstraint("share_den > 0", name="ck_share_den_positive"),
    )


class UssdSession(Base):
    """State for the *444# fallback — farmers without smartphones."""

    __tablename__ = "ussd_session"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    msisdn_hash: Mapped[str] = mapped_column(String(64), index=True)
    language: Mapped[str] = mapped_column(String(4), default="ps")
    menu_state: Mapped[str] = mapped_column(String(48), default="ROOT")
    buffer: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_input_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
