-- ============================================================================
-- 002_append_only.sql — make audit_log genuinely append-only.
--
-- A hash chain whose table accepts UPDATE or DELETE is decoration. This
-- migration is the single most important file in the repository: if it is not
-- applied, every integrity claim the system makes is void.
--
-- WHY TRIGGERS ARE NOT ENOUGH ON THEIR OWN
--   A BEFORE UPDATE trigger can be dropped by anyone with DDL rights on the
--   table — which in most government deployments includes the DBA who is
--   exactly the adversary we are defending against. So we do three things:
--
--   1. Rules + triggers that refuse UPDATE/DELETE (defence against mistakes
--      and against an attacker who does not notice the protection).
--   2. REVOKE of UPDATE/DELETE from the application role, so the running
--      service physically cannot rewrite history even if the code has a bug.
--   3. An append-only escrow: corrections go through a SECURITY DEFINER
--      function owned by a separate role, which writes a *superseding* entry
--      rather than modifying the old one, and records who asked and why.
--
-- The third point matters legally. A Patwari will eventually need to correct a
-- genuine clerical error. If the answer is "the database forbids it", someone
-- will disable the database. If the answer is "write a correction entry, the
-- same way a Fard Badar works on paper", the system survives contact with
-- reality — and the original entry remains for the court to see.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Refuse modification
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log is append-only: % is forbidden. Write a new entry instead.',
        TG_OP
        USING HINT = 'Corrections must be recorded as a new superseding event.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log;
CREATE TRIGGER trg_audit_log_no_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

-- Rules catch the case where a trigger is bypassed with ALTER TABLE ... DISABLE
-- TRIGGER, which is a common "temporary fix" that becomes permanent.
DROP RULE IF EXISTS audit_log_no_update ON audit_log;
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
DROP RULE IF EXISTS audit_log_no_delete ON audit_log;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

-- Anchors are append-only for the same reason: someone who can rewrite the
-- published Merkle root has defeated the anchoring defence entirely.
CREATE OR REPLACE FUNCTION chain_anchor_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'chain_anchor is append-only: % is forbidden.', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chain_anchor_append_only ON chain_anchor;
CREATE TRIGGER trg_chain_anchor_append_only
    BEFORE UPDATE OR DELETE ON chain_anchor
    FOR EACH ROW EXECUTE FUNCTION chain_anchor_append_only();

DROP RULE IF EXISTS chain_anchor_no_update ON chain_anchor;
CREATE RULE chain_anchor_no_update AS ON UPDATE TO chain_anchor DO INSTEAD NOTHING;
DROP RULE IF EXISTS chain_anchor_no_delete ON chain_anchor;
CREATE RULE chain_anchor_no_delete AS ON DELETE TO chain_anchor DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Take the privilege away from the application
-- ---------------------------------------------------------------------------
-- Run this as the migration owner. Adjust the role name to match your
-- deployment; the point is that the role the API uses must not hold these.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klri_app') THEN
        EXECUTE 'REVOKE UPDATE, DELETE ON audit_log FROM klri_app';
        EXECUTE 'REVOKE UPDATE, DELETE ON chain_anchor FROM klri_app';
        EXECUTE 'GRANT INSERT, SELECT ON audit_log TO klri_app';
        EXECUTE 'GRANT INSERT, SELECT ON chain_anchor TO klri_app';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. The lawful correction path
-- ---------------------------------------------------------------------------
-- Records a superseding correction event. It never touches the original row.

CREATE TABLE IF NOT EXISTS audit_log_correction (
    id             BIGSERIAL PRIMARY KEY,
    chain_key      TEXT NOT NULL,
    supersedes_seq BIGINT NOT NULL,
    correction_seq BIGINT,
    requested_by   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    approved_by    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_correction_reason CHECK (length(btrim(reason)) >= 20)
);

CREATE OR REPLACE FUNCTION record_correction(
    p_chain_key     TEXT,
    p_supersedes_seq BIGINT,
    p_event_type    TEXT,
    p_entity_type   TEXT,
    p_entity_id     TEXT,
    p_actor_id      TEXT,
    p_actor_role    TEXT,
    p_payload       JSONB,
    p_reason        TEXT,
    p_approved_by   TEXT DEFAULT NULL
) RETURNS TABLE (correction_id BIGINT, ledger_seq BIGINT, ledger_hash TEXT) AS $$
DECLARE
    v_prev      CHAR(64);
    v_seq       BIGINT;
    v_hash      CHAR(64);
    v_payload   JSONB;
    v_corr_id   BIGINT;
    v_now       TIMESTAMPTZ := now();
BEGIN
    IF length(btrim(coalesce(p_reason, ''))) < 20 THEN
        RAISE EXCEPTION 'a correction needs a written reason of at least 20 characters';
    END IF;

    SELECT entry_hash INTO v_prev
      FROM audit_log
     WHERE chain_key = p_chain_key
     ORDER BY seq DESC
     LIMIT 1;

    SELECT coalesce(max(seq), 0) + 1 INTO v_seq
      FROM audit_log
     WHERE chain_key = p_chain_key;

    v_payload := p_payload || jsonb_build_object(
        'supersedes_seq', p_supersedes_seq,
        'correction_reason', p_reason,
        'approved_by', p_approved_by);

    -- Mirrors app/core/hashchain.py::compute_entry_hash. If you change the
    -- pre-image here you must change it there too, or every entry will verify
    -- as tampered.
    v_hash := encode(digest(
        convert_to(
            jsonb_build_object(
                'v', 'kp-canonical-1',
                'chain', p_chain_key,
                'seq', v_seq,
                'prev', coalesce(v_prev, repeat('0', 64)),
                'event', p_event_type,
                'entity_type', p_entity_type,
                'entity_id', p_entity_id,
                'actor', p_actor_id,
                'occurred_at', to_char(v_now AT TIME ZONE 'UTC',
                                       'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'payload', v_payload
            )::text || to_char(v_now AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            'UTF8'),
        'sha256'), 'hex');

    INSERT INTO audit_log (seq, chain_key, prev_hash, entry_hash, event_type,
                           entity_type, entity_id, actor_id, actor_role,
                           payload, occurred_at, created_at)
    VALUES (v_seq, p_chain_key, coalesce(v_prev, repeat('0', 64)), v_hash,
            p_event_type, p_entity_type, p_entity_id, p_actor_id, p_actor_role,
            v_payload, v_now, v_now);

    INSERT INTO audit_log_correction (chain_key, supersedes_seq, correction_seq,
                                      requested_by, reason, approved_by)
    VALUES (p_chain_key, p_supersedes_seq, v_seq, p_actor_id, p_reason, p_approved_by)
    RETURNING id INTO v_corr_id;

    RETURN QUERY SELECT v_corr_id, v_seq, v_hash::text;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMENT ON FUNCTION record_correction IS
    'The only supported way to correct a ledger entry. Appends a superseding '
    'event; never modifies or removes the original.';

COMMIT;
