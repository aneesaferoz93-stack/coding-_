"""Hash-chain tests, including the attacks it is supposed to catch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.canonical import canonical_json, canonical_sha256
from app.core.hashchain import (
    GENESIS_HASH,
    LedgerEntry,
    TamperKind,
    compute_entry_hash,
    merkle_proof,
    merkle_root,
    sign_anchor,
    verify_anchor_signature,
    verify_entries,
    verify_merkle_proof,
)

NOW = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)


def entry(seq: int, prev: str, payload: dict, **kw) -> LedgerEntry:
    e = LedgerEntry(
        seq=seq, prev_hash=prev, entry_hash="", chain_key="test",
        event_type="MUTATION_DRAFTED", entity_type="mutation",
        entity_id=kw.pop("entity_id", f"M-{seq}"), actor_id="patwari-1", actor_role="PATWARI",
        payload=payload, occurred_at=NOW + timedelta(minutes=seq), **kw)
    e.entry_hash = e.recompute()
    return e


def chain(n: int) -> list[LedgerEntry]:
    out: list[LedgerEntry] = []
    prev = GENESIS_HASH
    for i in range(1, n + 1):
        e = entry(i, prev, {"i": i})
        out.append(e)
        prev = e.entry_hash
    return out


# --------------------------------------------------------------------------
# canonicalisation
# --------------------------------------------------------------------------

def test_canonical_is_stable_across_key_order():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_canonical_drops_nulls():
    assert canonical_json({"a": 1, "b": None}) == canonical_json({"a": 1})


def test_canonical_handles_urdu_and_pashto_identically():
    assert canonical_sha256({"name": "ګل محمد خان"}) == canonical_sha256({"name": "ګل محمد خان"})
    assert canonical_sha256({"name": "ګل"}) != canonical_sha256({"name": "گل"})


def test_canonical_float_precision_is_bounded():
    # Two GPS fixes that differ only in float noise must hash the same.
    assert canonical_sha256({"lat": 34.0151234}) == canonical_sha256({"lat": 34.0151234000001})


def test_canonical_rejects_nan():
    from app.core.canonical import CanonicalisationError
    with pytest.raises(CanonicalisationError):
        canonical_json({"x": float("nan")})


# --------------------------------------------------------------------------
# chain integrity
# --------------------------------------------------------------------------

def test_clean_chain_verifies():
    result = verify_entries(chain(10), chain_key="test")
    assert result.intact, [f.detail for f in result.findings]
    assert result.entries_checked == 10


def test_empty_chain_is_intact():
    assert verify_entries([], chain_key="test").intact


def test_payload_edit_is_detected():
    """The core promise: edit a record, and verification says so."""
    entries = chain(5)
    original = entries[2].entry_hash
    entries[2].payload["declared_price_pkr"] = "999999999"   # the forgery
    result = verify_entries(entries, chain_key="test")
    assert not result.intact
    assert any(f.kind is TamperKind.HASH_MISMATCH and f.seq == 3 for f in result.findings)
    # The stored hash must not have changed — only the recomputation differs.
    assert entries[2].entry_hash == original


def test_deleted_row_is_detected_as_gap():
    entries = chain(6)
    del entries[3]                       # seq 4 removed
    result = verify_entries(entries, chain_key="test")
    assert any(f.kind is TamperKind.GAP for f in result.findings)
    assert not result.intact


def test_splice_is_detected_as_broken_link():
    entries = chain(5)
    entries[3].prev_hash = "a" * 64
    result = verify_entries(entries, chain_key="test")
    assert any(f.kind is TamperKind.BROKEN_LINK for f in result.findings)


def test_backdated_entry_is_detected():
    """Within one record's history, time must not run backwards."""
    entries = []
    prev = GENESIS_HASH
    for i in range(1, 5):
        e = entry(i, prev, {"i": i}, entity_id="M-1")   # same record
        entries.append(e)
        prev = e.entry_hash
    entries[2].occurred_at = NOW - timedelta(days=5)
    result = verify_entries(entries, chain_key="test")
    assert any(f.kind is TamperKind.CLOCK_ROLLBACK and f.seq == 3 for f in result.findings)


def test_out_of_order_event_times_across_records_are_not_flagged():
    """e-Fard can report an older mutation after we decided a newer one.

    That is normal, not tampering — which is why monotonicity is only enforced
    within a single record's history.
    """
    older = entry(2, GENESIS_HASH, {"i": 2}, entity_id="M-OLDER")
    newer = entry(1, GENESIS_HASH, {"i": 1}, entity_id="M-NEWER")
    result = verify_entries([older, newer], chain_key="test")
    # The hashes will not match a real chain here (both claim prev=genesis), so
    # assert only on the absence of a clock finding.
    assert not any(f.kind is TamperKind.CLOCK_ROLLBACK for f in result.findings)


def test_swapped_entity_identity_is_detected():
    """Keep the payload, change which khasra it refers to."""
    entries = chain(3)
    entries[1].entity_id = "M-999"
    result = verify_entries(entries, chain_key="test")
    assert any(f.kind is TamperKind.HASH_MISMATCH for f in result.findings)


def test_truncation_below_anchor_is_detected():
    entries = chain(10)
    anchors = [{"from_seq": 1, "to_seq": 10,
                "merkle_root": merkle_root([e.entry_hash for e in entries]),
                "entry_count": 10, "signature": None}]
    # Attacker deletes the last three entries and everything after them.
    result = verify_entries(entries[:7], chain_key="test", anchors=anchors)
    assert any(f.kind is TamperKind.TRUNCATED for f in result.findings)


def test_anchor_root_mismatch_is_detected():
    entries = chain(6)
    anchors = [{"from_seq": 1, "to_seq": 6, "merkle_root": "b" * 64,
                "entry_count": 6, "signature": None}]
    result = verify_entries(entries, chain_key="test", anchors=anchors)
    assert any(f.kind is TamperKind.ANCHOR_MISMATCH for f in result.findings)


def test_valid_anchor_passes():
    entries = chain(6)
    anchors = [{"from_seq": 1, "to_seq": 6,
                "merkle_root": merkle_root([e.entry_hash for e in entries]),
                "entry_count": 6, "signature": None}]
    result = verify_entries(entries, chain_key="test", anchors=anchors)
    assert result.intact


def test_anchor_signature_verification():
    payload = canonical_json({"chain": "kp-main", "from_seq": 1, "to_seq": 5})
    sig = sign_anchor(payload, "secret-key")
    assert verify_anchor_signature(payload, sig, "secret-key")
    assert not verify_anchor_signature(payload, sig, "wrong-key")
    assert not verify_anchor_signature(payload + " ", sig, "secret-key")


# --------------------------------------------------------------------------
# Merkle proofs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 9, 17])
def test_merkle_proof_for_every_index(n):
    hashes = [f"{i:064x}" for i in range(n)]
    root = merkle_root(hashes)
    for i in range(n):
        proof = merkle_proof(hashes, i)
        assert verify_merkle_proof(hashes[i], proof, root), f"proof failed n={n} i={i}"


def test_merkle_proof_rejects_a_forged_leaf():
    hashes = [f"{i:064x}" for i in range(8)]
    root = merkle_root(hashes)
    assert not verify_merkle_proof("f" * 64, merkle_proof(hashes, 3), root)


def test_compute_entry_hash_binds_every_identity_field():
    base = dict(seq=1, prev_hash=GENESIS_HASH, chain_key="c", event_type="E",
                entity_type="mutation", entity_id="M1", actor_id="A",
                occurred_at=NOW, payload={"x": 1})
    h = compute_entry_hash(**base)
    for field, value in [("actor_id", "B"), ("entity_id", "M2"),
                         ("chain_key", "other"), ("event_type", "F")]:
        assert compute_entry_hash(**{**base, field: value}) != h
