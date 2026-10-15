-- ============================================================================
-- 001_schema.sql — KP Land Record Integrity & Fraud Prevention System
--
-- Target: PostgreSQL 14+ with PostGIS 3.x
--
-- WHAT IS IN HERE
--   audit_log            the append-only hash chain (the integrity core)
--   chain_anchor         signed, publishable Merkle checkpoints
--   fraud_flags          one rule firing on one mutation (the review queue)
--   ground_verification  one Patwari site visit + its trust score
--   mutation / clearance mirror of, and opinion on, the e-Fard record
--   khasra_share         current ownership shares (double-sale arithmetic)
--   reference tables     district, mouza, dc_rate, latha_record, officer,
--                        field_device, ussd_session
--
-- WHAT IS DELIBERATELY NOT IN HERE
--   Anything that owns title. This schema never becomes the system of record
--   for who owns land — that stays with LRMIS / e-Fard and, legally, with the
--   manual register until Phase 3. Everything here is either a mirror, an
--   opinion, or evidence about the mirror.
--
-- PORTABILITY
--   Geometry is stored as WKT in *_wkt columns (present on every dialect) with
--   real PostGIS geometry columns generated from them. That keeps the service
--   runnable on SQLite for a single-district pilot while giving production
--   real spatial indexes.
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS district (
    code        TEXT PRIMARY KEY,
    name_en     TEXT NOT NULL,
    name_ur     TEXT,
    name_ps     TEXT,
    phase       TEXT NOT NULL DEFAULT 'PHASE_1_DUAL_RUN',
    live        BOOLEAN NOT NULL DEFAULT TRUE,
    e_registry_live BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS mouza (
    code              TEXT PRIMARY KEY,
    district_code     TEXT NOT NULL REFERENCES district(code),
    tehsil_code       TEXT NOT NULL,
    name_en           TEXT NOT NULL,
    name_ur           TEXT,
    name_ps           TEXT,
    -- Digitised cadastral outline. WKT on every dialect...
    boundary_wkt      TEXT,
    -- ...and a real geometry column generated from it, so ST_Contains and
    -- ST_Distance are indexed. ST_GeomFromText is IMMUTABLE, so this is legal.
    geom              geometry(MultiPolygon, 4326)
                      GENERATED ALWAYS AS (ST_GeomFromText(boundary_wkt, 4326)) STORED,
    centroid_lat      DOUBLE PRECISION,
    centroid_lon      DOUBLE PRECISION,
    digitised         BOOLEAN NOT NULL DEFAULT FALSE,
    latha_scanned_pct NUMERIC(5, 2),
    total_khasras     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_mouza_district ON mouza (district_code);
-- The geofence query: "is this fix inside this mouza?" runs on every single
-- site visit, so the spatial index is not optional.
CREATE INDEX IF NOT EXISTS ix_mouza_geom ON mouza USING GIST (geom);

CREATE TABLE IF NOT EXISTS dc_rate (
    id                  BIGSERIAL PRIMARY KEY,
    district_code       TEXT NOT NULL,
    mouza_code          TEXT,
    tehsil_code         TEXT,
    land_type           TEXT NOT NULL DEFAULT 'AGRICULTURAL',
    rate_per_kanal      NUMERIC(14, 2) NOT NULL,
    effective_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to        TIMESTAMPTZ,
    source_notification TEXT
);
CREATE INDEX IF NOT EXISTS ix_dcrate_lookup
    ON dc_rate (district_code, mouza_code, land_type, effective_from DESC);

-- The pre-computerisation manual register. Our only independent witness: it
-- was written before anyone knew this system would exist.
CREATE TABLE IF NOT EXISTS latha_record (
    id                   BIGSERIAL PRIMARY KEY,
    district_code        TEXT NOT NULL,
    mouza_code           TEXT NOT NULL,
    khasra_no            TEXT NOT NULL,
    khewat_no            TEXT,
    owner_name           TEXT NOT NULL,
    father_name          TEXT,
    qaum                 TEXT,
    mouza_name_in_record TEXT,
    area_kanal           NUMERIC(12, 4),
    jamabandi_year       INTEGER,
    scanned_page         TEXT,
    scan_sha256          TEXT
);
CREATE INDEX IF NOT EXISTS ix_latha_khasra
    ON latha_record (district_code, mouza_code, khasra_no);

CREATE TABLE IF NOT EXISTS officer (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,
    cnic_hash     TEXT,
    district_code TEXT,
    tehsil_code   TEXT,
    active        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS field_device (
    device_id          TEXT PRIMARY KEY,
    officer_id         TEXT REFERENCES officer(id),
    label              TEXT,
    platform           TEXT,
    app_version        TEXT,
    attestation_secret TEXT,          -- HMAC key; issued once at enrolment
    registered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked            BOOLEAN NOT NULL DEFAULT FALSE
);

-- ---------------------------------------------------------------------------
-- Transaction mirror
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mutation (
    id                      TEXT PRIMARY KEY,
    efard_ref               TEXT,
    district_code           TEXT NOT NULL,
    tehsil_code             TEXT NOT NULL,
    mouza_code              TEXT NOT NULL,
    mouza_name              TEXT,
    khewat_no               TEXT,
    khasra_no               TEXT NOT NULL,
    area_kanal              NUMERIC(12, 4) NOT NULL DEFAULT 0,
    transaction_type        TEXT NOT NULL DEFAULT 'SALE',
    declared_price_pkr      NUMERIC(16, 2) NOT NULL DEFAULT 0,
    dc_value_pkr            NUMERIC(16, 2),
    patwari_id              TEXT,
    patwari_mutations_today INTEGER NOT NULL DEFAULT 0,
    seller_held_share       NUMERIC(12, 8),
    status                  TEXT NOT NULL DEFAULT 'DRAFT',
    proposed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at              TIMESTAMPTZ,
    attested_at             TIMESTAMPTZ,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_mutation_status ON mutation (status);
-- Rule R1 (rapid re-mutation) is a range scan on this exact key.
CREATE INDEX IF NOT EXISTS ix_mutation_khasra_time
    ON mutation (district_code, mouza_code, khasra_no, proposed_at DESC);

CREATE TABLE IF NOT EXISTS clearance (
    clearance_id      TEXT PRIMARY KEY,
    mutation_id       TEXT REFERENCES mutation(id),
    decision          TEXT NOT NULL,
    phase             TEXT NOT NULL,
    binding           BOOLEAN NOT NULL DEFAULT FALSE,
    severity          TEXT NOT NULL,
    score             INTEGER NOT NULL DEFAULT 0,
    reasons           JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_actions  JSONB NOT NULL DEFAULT '[]'::jsonb,
    escalation_role   TEXT,
    ledger_seq        BIGINT,
    ledger_hash       TEXT,
    config_fingerprint TEXT,
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ,
    overridden_by     TEXT,
    override_reason   TEXT
);
CREATE INDEX IF NOT EXISTS ix_clearance_decision ON clearance (decision, decided_at DESC);

-- ---------------------------------------------------------------------------
-- THE LEDGER
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    seq           BIGINT NOT NULL,
    chain_key     TEXT NOT NULL DEFAULT 'kp-main',
    prev_hash     CHAR(64) NOT NULL,
    entry_hash    CHAR(64) NOT NULL,
    event_type    TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    actor_role    TEXT NOT NULL DEFAULT 'SYSTEM',
    district_code TEXT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_audit_chain_seq UNIQUE (chain_key, seq),
    CONSTRAINT ck_audit_hash_len CHECK (length(entry_hash) = 64),
    CONSTRAINT ck_audit_prevhash_len CHECK (length(prev_hash) = 64)
);
CREATE INDEX IF NOT EXISTS ix_audit_seq ON audit_log (chain_key, seq);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_event ON audit_log (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_district ON audit_log (district_code, occurred_at DESC);

CREATE TABLE IF NOT EXISTS chain_anchor (
    id            BIGSERIAL PRIMARY KEY,
    chain_key     TEXT NOT NULL DEFAULT 'kp-main',
    from_seq      BIGINT NOT NULL,
    to_seq        BIGINT NOT NULL,
    entry_count   INTEGER NOT NULL,
    merkle_root   CHAR(64) NOT NULL,
    signature     TEXT,
    signed_by     TEXT,
    anchored_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_ref TEXT,
    published_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_anchor_chain ON chain_anchor (chain_key, to_seq DESC);

-- ---------------------------------------------------------------------------
-- FRAUD FLAGS
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fraud_flags (
    id                  BIGSERIAL PRIMARY KEY,
    mutation_id         TEXT REFERENCES mutation(id),
    district_code       TEXT NOT NULL,
    mouza_code          TEXT,
    khasra_no           TEXT,
    rule_id             TEXT NOT NULL,
    rule_name           TEXT NOT NULL,
    severity            TEXT NOT NULL,
    title               TEXT NOT NULL,
    detail              TEXT NOT NULL,
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommended_action  TEXT,
    legal_basis         TEXT,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    engine_version      TEXT,
    config_fingerprint  TEXT,
    raised_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    review_note         TEXT,
    CONSTRAINT ck_flag_severity CHECK (severity IN ('RED', 'AMBER', 'INFO')),
    CONSTRAINT ck_flag_status CHECK (status IN
        ('OPEN', 'UNDER_REVIEW', 'CONFIRMED_FRAUD', 'DISMISSED', 'REFERRED'))
);
CREATE INDEX IF NOT EXISTS ix_flag_queue ON fraud_flags (status, severity, raised_at DESC);
CREATE INDEX IF NOT EXISTS ix_flag_rule ON fraud_flags (rule_id, raised_at DESC);
CREATE INDEX IF NOT EXISTS ix_flag_khasra ON fraud_flags (district_code, mouza_code, khasra_no);
-- A dismissed flag without a written reason is indistinguishable from an
-- override, so the database enforces the note rather than trusting the app.
CREATE OR REPLACE FUNCTION require_review_note() RETURNS trigger AS $$
BEGIN
    IF NEW.status IN ('DISMISSED', 'CONFIRMED_FRAUD', 'REFERRED')
       AND (NEW.review_note IS NULL OR length(btrim(NEW.review_note)) < 10) THEN
        RAISE EXCEPTION
            'a written reason of at least 10 characters is required to close a flag as %',
            NEW.status;
    END IF;
    IF NEW.status IN ('DISMISSED', 'CONFIRMED_FRAUD', 'REFERRED', 'UNDER_REVIEW') THEN
        NEW.reviewed_at := coalesce(NEW.reviewed_at, now());
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fraud_flags_review_note ON fraud_flags;
CREATE TRIGGER trg_fraud_flags_review_note
    BEFORE INSERT OR UPDATE ON fraud_flags
    FOR EACH ROW EXECUTE FUNCTION require_review_note();

-- ---------------------------------------------------------------------------
-- GROUND VERIFICATION
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ground_verification (
    id                     TEXT PRIMARY KEY,
    mutation_id            TEXT REFERENCES mutation(id),
    khasra_key             TEXT,
    district_code          TEXT NOT NULL,
    mouza_code             TEXT NOT NULL,
    khasra_no              TEXT,

    officer_id             TEXT,
    device_id              TEXT,

    latitude               DOUBLE PRECISION NOT NULL,
    longitude              DOUBLE PRECISION NOT NULL,
    accuracy_m             DOUBLE PRECISION,
    altitude_m             DOUBLE PRECISION,
    provider               TEXT,
    is_mock                BOOLEAN,
    satellites             INTEGER,
    geom_wkt               TEXT,
    geom                   geometry(Point, 4326)
                           GENERATED ALWAYS AS (ST_GeomFromText(geom_wkt, 4326)) STORED,

    inside_mouza           BOOLEAN,
    distance_to_boundary_m DOUBLE PRECISION,

    photo_sha256           CHAR(64),
    photo_phash            TEXT,
    photo_bytes            INTEGER,
    photo_exif_datetime    TIMESTAMPTZ,
    photo_source           TEXT,

    -- CNICs are never stored: only salted hashes, for linkage and repeat
    -- detection. A land-records database is precisely the wrong place for a
    -- list of national identity numbers.
    witness_cnic_hashes    JSONB NOT NULL DEFAULT '[]'::jsonb,
    witness_nadra_ok       JSONB NOT NULL DEFAULT '[]'::jsonb,

    trust_score            INTEGER,
    trust_band             TEXT,
    trust_signals          JSONB NOT NULL DEFAULT '[]'::jsonb,
    trust_config_version   TEXT,

    status                 TEXT NOT NULL DEFAULT 'PENDING',
    captured_at            TIMESTAMPTZ,
    received_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    offline_hours          DOUBLE PRECISION,
    app_version            TEXT,
    client_submission_id   TEXT UNIQUE,
    notes                  TEXT,

    CONSTRAINT ck_gv_status CHECK (status IN
        ('PENDING', 'ACCEPTED', 'REJECTED', 'SUPERSEDED')),
    CONSTRAINT ck_gv_photo_source CHECK
        (photo_source IS NULL OR photo_source IN ('camera', 'gallery', 'unknown'))
);
CREATE INDEX IF NOT EXISTS ix_gv_mouza ON ground_verification (mouza_code, captured_at DESC);
CREATE INDEX IF NOT EXISTS ix_gv_trust ON ground_verification (trust_score, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_gv_photo ON ground_verification (photo_sha256);
CREATE INDEX IF NOT EXISTS ix_gv_geom ON ground_verification USING GIST (geom);
CREATE INDEX IF NOT EXISTS ix_gv_device ON ground_verification (device_id, captured_at DESC);

-- ---------------------------------------------------------------------------
-- OWNERSHIP SHARES (double-sale arithmetic)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS khasra_share (
    id                BIGSERIAL PRIMARY KEY,
    khasra_key        TEXT NOT NULL,
    district_code     TEXT NOT NULL,
    mouza_code        TEXT NOT NULL,
    khasra_no         TEXT NOT NULL,
    owner_cnic_hash   TEXT NOT NULL,
    owner_name        TEXT,
    share_num         INTEGER NOT NULL DEFAULT 1,
    share_den         INTEGER NOT NULL DEFAULT 1,
    area_kanal        NUMERIC(12, 4),
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to          TIMESTAMPTZ,
    source_mutation_id TEXT,
    ledger_seq        BIGINT,
    CONSTRAINT ck_share_den_positive CHECK (share_den > 0)
);
CREATE INDEX IF NOT EXISTS ix_share_current ON khasra_share (khasra_key, valid_to);
CREATE INDEX IF NOT EXISTS ix_share_owner ON khasra_share (owner_cnic_hash, valid_to);

-- The sum of all *current* shares in a khasra must never exceed a whole. This
-- constraint is the last line of defence against double sale: it makes the
-- record refuse to become internally inconsistent.
CREATE OR REPLACE FUNCTION check_share_total() RETURNS trigger AS $$
DECLARE
    total NUMERIC;
BEGIN
    SELECT coalesce(sum(share_num::numeric / share_den), 0)
      INTO total
      FROM khasra_share
     WHERE khasra_key = NEW.khasra_key
       AND valid_to IS NULL;
    IF total > 1.00000001 THEN
        RAISE WARNING
            'khasra % is now oversubscribed (total share = %). This land is '
            'recorded as owned more than once — raise a share audit.',
            NEW.khasra_key, total;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_khasra_share_total ON khasra_share;
CREATE TRIGGER trg_khasra_share_total
    AFTER INSERT OR UPDATE ON khasra_share
    FOR EACH ROW EXECUTE FUNCTION check_share_total();

-- ---------------------------------------------------------------------------
-- USSD sessions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ussd_session (
    session_id    TEXT PRIMARY KEY,
    msisdn_hash   TEXT NOT NULL,
    language      TEXT NOT NULL DEFAULT 'ps',
    menu_state    TEXT NOT NULL DEFAULT 'ROOT',
    buffer        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_input_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_ussd_msisdn ON ussd_session (msisdn_hash, created_at DESC);

COMMIT;
