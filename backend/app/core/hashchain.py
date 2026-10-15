"""
hashchain.py — append-only, tamper-evident ledger for land record events.

Design goal: give a Patwari, a Tehsildar and a civil court the ability to prove
that a mutation record has (or has not) been altered since it was written —
*without* running a blockchain.

What this actually is
---------------------
A hash chain (Merkle-style linked list) with three defences:

  A. LINKED HASHES
     Every entry stores ``prev_hash`` and ``entry_hash`` where

         entry_hash = SHA256(prev_hash || canonical(payload) || seq
                             || event_type || entity || occurred_at || actor)

     Change one byte of a payload and every hash after it changes.

  B. MONOTONIC SEQUENCE
     ``seq`` is a gap-free counter per chain. Deleting a row (or an entire tail)
     leaves a hole that verification reports as ``GAP`` / ``TRUNCATION``.

  C. EXTERNAL ANCHORING
     Periodically we freeze a range [from_seq, to_seq] into a signed
     ``chain_anchor`` containing a Merkle root, and publish that root outside
     the database (Gazette notice,BoR website, newspaper, mirror). An attacker
     with DBA rights can rewrite the table, but they cannot rewrite every
     published copy of the root. This is the part that makes "blockchain-like"
     honest rather than marketing: the trust comes from *publication*, not from
     consensus.

Why not a real blockchain
-------------------------
Consensus is for parties who do not trust each other and have no referee. Here
there *is* a referee (the Board of Revenue) and the adversary is an insider
with DB access, not a Sybil network. A blockchain would add cost, latency and
an energy/governance burden while defending against a threat we do not have.
See docs/05-threat-model.md.

Concurrency
-----------
Appends take a Postgres advisory lock on the chain key, or serialise on the
chain's ``head`` row with ``SELECT ... FOR UPDATE`` on SQLite. Two concurrent
mutations in the same chain cannot mint the same ``seq``.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .canonical import CANONICAL_VERSION, canonical_json

GENESIS_HASH = "0" * 64
DEFAULT_CHAIN = "kp-main"


class LedgerEvent(str, Enum):
    MUTATION_DRAFTED = "MUTATION_DRAFTED"
    MUTATION_CLEARED = "MUTATION_CLEARED"
    MUTATION_HELD = "MUTATION_HELD"
    MUTATION_BLOCKED = "MUTATION_BLOCKED"
    MUTATION_ATTESTED = "MUTATION_ATTESTED"
    MUTATION_REJECTED = "MUTATION_REJECTED"
    MUTATION_AMENDED = "MUTATION_AMENDED"          # the legal correction path
    FARD_ISSUED = "FARD_ISSUED"
    FARD_MIRRORED = "FARD_MIRRORED"                # observed from e-Fard, not ours
    GROUND_VERIFICATION = "GROUND_VERIFICATION"
    GROUND_REJECTED = "GROUND_REJECTED"
    WITNESS_ATTESTED = "WITNESS_ATTESTED"
    FRAUD_FLAG_RAISED = "FRAUD_FLAG_RAISED"
    FRAUD_FLAG_CLEARED = "FRAUD_FLAG_CLEARED"
    RECORD_TAMPER_DETECTED = "RECORD_TAMPER_DETECTED"
    SHARE_TRANSFER = "SHARE_TRANSFER"
    CHAIN_ANCHORED = "CHAIN_ANCHORED"
    POLICY_CHANGED = "POLICY_CHANGED"
    OFFICER_ACTION = "OFFICER_ACTION"


class TamperKind(str, Enum):
    OK = "OK"
    HASH_MISMATCH = "HASH_MISMATCH"        # payload edited in place
    BROKEN_LINK = "BROKEN_LINK"            # prev_hash does not match predecessor
    GAP = "GAP"                            # missing seq numbers (deleted rows)
    TRUNCATED = "TRUNCATED"                # chain shorter than last anchor
    BAD_GENESIS = "BAD_GENESIS"
    ANCHOR_MISMATCH = "ANCHOR_MISMATCH"    # recomputed Merkle root != stored root
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    CLOCK_ROLLBACK = "CLOCK_ROLLBACK"      # occurred_at earlier than predecessor


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_entry_hash(
    *,
    seq: int,
    prev_hash: str,
    chain_key: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor_id: str,
    occurred_at: datetime,
    payload: Any,
) -> str:
    """Deterministic SHA-256 over one logical ledger entry.

    The pre-image deliberately binds *all* identity fields, not just the
    payload. Otherwise an attacker could keep a valid hash while swapping which
    khasra or which officer an entry refers to.
    """
    pre_image = {
        "v": CANONICAL_VERSION,
        "chain": chain_key,
        "seq": seq,
        "prev": prev_hash,
        "event": event_type,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "actor": actor_id,
        "occurred_at": occurred_at,
        "payload": payload,
    }
    return hashlib.sha256(canonical_json(pre_image).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class LedgerEntry:
    """Dialect-free representation of one row of ``audit_log``."""

    seq: int
    prev_hash: str
    entry_hash: str
    chain_key: str
    event_type: str
    entity_type: str
    entity_id: str
    actor_id: str
    actor_role: str
    district_code: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=_utcnow)
    created_at: datetime = field(default_factory=_utcnow)
    id: int | None = None

    def recompute(self) -> str:
        return compute_entry_hash(
            seq=self.seq,
            prev_hash=self.prev_hash,
            chain_key=self.chain_key,
            event_type=self.event_type,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            actor_id=self.actor_id,
            occurred_at=self.occurred_at,
            payload=self.payload,
        )


# --------------------------------------------------------------------------
# Merkle helpers
# --------------------------------------------------------------------------

def merkle_root(hashes: Sequence[str]) -> str:
    """Bitcoin-style Merkle root (duplicate-last on odd levels).

    Lets us prove that a single entry was inside an anchored range without
    republishing the whole range.
    """
    if not hashes:
        return GENESIS_HASH
    level = [h.lower() for h in hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            hashlib.sha256(bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])).hexdigest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


def merkle_proof(hashes: Sequence[str], index: int) -> list[dict[str, str]]:
    """Return the sibling path needed to prove ``hashes[index]`` is in the root."""
    if not (0 <= index < len(hashes)):
        raise IndexError("index out of range")
    level = [h.lower() for h in hashes]
    proof: list[dict[str, str]] = []
    idx = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        sibling_idx = idx + 1 if idx % 2 == 0 else idx - 1
        proof.append({"position": "right" if sibling_idx > idx else "left",
                      "hash": level[sibling_idx]})
        level = [
            hashlib.sha256(bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1])).hexdigest()
            for i in range(0, len(level), 2)
        ]
        idx //= 2
    return proof


def verify_merkle_proof(leaf: str, proof: Iterable[dict[str, str]], root: str) -> bool:
    """Verify a proof produced by :func:`merkle_proof`."""
    acc = leaf.lower()
    for step in proof:
        sibling = step["hash"].lower()
        if step.get("position") == "left":
            acc = hashlib.sha256(bytes.fromhex(sibling) + bytes.fromhex(acc)).hexdigest()
        else:
            acc = hashlib.sha256(bytes.fromhex(acc) + bytes.fromhex(sibling)).hexdigest()
    return acc == root.lower()


def sign_anchor(payload: str, signing_key: str) -> str:
    """HMAC-SHA256 over an anchor payload.

    In production this function is the seam where an HSM / PKCS#11 signature
    (or an Ed25519 key held by the Board of Revenue) is plugged in. The rest of
    the system only ever compares signatures, never produces them ad hoc.
    """
    return hmac.new(signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_anchor_signature(payload: str, signature: str, signing_key: str) -> bool:
    return hmac.compare_digest(sign_anchor(payload, signing_key), signature)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

@dataclass(slots=True)
class TamperFinding:
    kind: TamperKind
    seq: int | None
    detail: str
    expected: str | None = None
    found: str | None = None


@dataclass(slots=True)
class ChainVerification:
    chain_key: str
    entries_checked: int
    intact: bool
    findings: list[TamperFinding] = field(default_factory=list)
    first_seq: int | None = None
    last_seq: int | None = None
    head_hash: str | None = None
    recomputed_root: str | None = None
    checked_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_key": self.chain_key,
            "entries_checked": self.entries_checked,
            "intact": self.intact,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "head_hash": self.head_hash,
            "recomputed_root": self.recomputed_root,
            "checked_at": self.checked_at,
            "findings": [
                {
                    "kind": f.kind.value,
                    "seq": f.seq,
                    "detail": f.detail,
                    "expected": f.expected,
                    "found": f.found,
                }
                for f in self.findings
            ],
        }


def verify_entries(
    entries: Sequence[LedgerEntry],
    *,
    chain_key: str = DEFAULT_CHAIN,
    anchors: Sequence[dict[str, Any]] | None = None,
    signing_key: str | None = None,
) -> ChainVerification:
    """Recompute an entire chain from scratch and report every divergence.

    Pure function: takes rows, returns a report. It is deliberately separated
    from SQL so it can be unit-tested, run against an evidence export, or run
    by a court-appointed auditor on a copy of the database they do not trust.
    """
    findings: list[TamperFinding] = []

    if not entries:
        return ChainVerification(chain_key=chain_key, entries_checked=0, intact=True,
                                 findings=findings)

    ordered = sorted(entries, key=lambda e: e.seq)
    prev_hash = GENESIS_HASH
    expected_seq = ordered[0].seq
    # Monotonicity is checked per entity, not globally: e-Fard can legitimately
    # report a mutation that was drafted before a decision we already recorded
    # for a different mutation, and flagging that would be noise. What must
    # never happen is one record's own history running backwards.
    last_occurred_by_entity: dict[tuple[str, str], datetime] = {}

    for entry in ordered:
        # (B) gap detection — deleted rows
        if entry.seq != expected_seq:
            missing = list(range(expected_seq, entry.seq))
            findings.append(TamperFinding(
                kind=TamperKind.GAP,
                seq=entry.seq,
                detail=(f"{len(missing)} ledger entr"
                        f"{'y' if len(missing) == 1 else 'ies'} missing "
                        f"(seq {missing[0]}..{missing[-1]}). Rows were deleted."),
                expected=str(expected_seq),
                found=str(entry.seq),
            ))
            expected_seq = entry.seq

        # genesis
        if entry.seq == 1 and entry.prev_hash != GENESIS_HASH:
            findings.append(TamperFinding(
                kind=TamperKind.BAD_GENESIS, seq=entry.seq,
                detail="First entry does not descend from the genesis hash.",
                expected=GENESIS_HASH, found=entry.prev_hash,
            ))

        # (A) link integrity
        if entry.prev_hash != prev_hash and entry.seq != 1:
            findings.append(TamperFinding(
                kind=TamperKind.BROKEN_LINK, seq=entry.seq,
                detail="prev_hash does not equal the previous entry's hash.",
                expected=prev_hash, found=entry.prev_hash,
            ))

        # payload integrity
        recomputed = entry.recompute()
        if recomputed != entry.entry_hash:
            findings.append(TamperFinding(
                kind=TamperKind.HASH_MISMATCH, seq=entry.seq,
                detail=("Stored hash does not match the record contents. "
                        "The payload, actor, entity or timestamp was altered "
                        "after the entry was written."),
                expected=entry.entry_hash, found=recomputed,
            ))

        # backdating: an entry inserted before its own predecessor's event time
        key = (entry.entity_type, entry.entity_id)
        prev_occurred = last_occurred_by_entity.get(key)
        if prev_occurred is not None and entry.occurred_at < prev_occurred:
            findings.append(TamperFinding(
                kind=TamperKind.CLOCK_ROLLBACK, seq=entry.seq,
                detail=(f"Entry for {entry.entity_type}/{entry.entity_id} is dated "
                        "earlier than the previous entry for the same record."),
                expected=prev_occurred.isoformat(),
                found=entry.occurred_at.isoformat(),
            ))
        last_occurred_by_entity[key] = entry.occurred_at

        prev_hash = entry.entry_hash
        expected_seq = entry.seq + 1

    # (C) anchor checks
    for anchor in anchors or []:
        lo = anchor.get("from_seq")
        hi = anchor.get("to_seq")
        if lo is None or hi is None:
            continue
        if ordered[-1].seq < hi:
            findings.append(TamperFinding(
                kind=TamperKind.TRUNCATED, seq=ordered[-1].seq,
                detail=(f"Anchored range {lo}-{hi} exists but the chain now ends "
                        f"at {ordered[-1].seq}. The tail has been removed."),
                expected=str(hi), found=str(ordered[-1].seq),
            ))
            continue
        # Recompute, not read: an attacker who rewrites a payload *and*
        # recomputes its entry_hash would slip past a root built from the stored
        # hashes. Recomputing means the only way to keep the published root
        # valid is to leave the record alone.
        in_range = [e.recompute() for e in ordered if lo <= e.seq <= hi]
        root = merkle_root(in_range)
        if root != anchor.get("merkle_root"):
            findings.append(TamperFinding(
                kind=TamperKind.ANCHOR_MISMATCH, seq=hi,
                detail=(f"Recomputed Merkle root for anchored range {lo}-{hi} "
                        f"does not match the published root."),
                expected=str(anchor.get("merkle_root")), found=root,
            ))
        if signing_key and anchor.get("signature"):
            payload = canonical_json({
                "chain": chain_key, "from_seq": lo, "to_seq": hi,
                "merkle_root": anchor.get("merkle_root"),
                "entry_count": anchor.get("entry_count"),
            })
            if not verify_anchor_signature(payload, anchor["signature"], signing_key):
                findings.append(TamperFinding(
                    kind=TamperKind.SIGNATURE_INVALID, seq=hi,
                    detail="Anchor signature does not verify against the BoR key.",
                ))

    return ChainVerification(
        chain_key=chain_key,
        entries_checked=len(ordered),
        intact=not findings,
        findings=findings,
        first_seq=ordered[0].seq,
        last_seq=ordered[-1].seq,
        head_hash=ordered[-1].entry_hash,
        recomputed_root=merkle_root([e.entry_hash for e in ordered]),
    )


def new_request_id() -> str:
    return uuid.uuid4().hex
