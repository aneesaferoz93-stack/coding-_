"""
ledger.py — the write path for the append-only chain.

Two rules govern everything here:

1. Nothing is ever updated or deleted. A correction is a *new* entry that
   supersedes the old one, exactly as a Fard Badar works on paper. This is the
   property that makes the audit log admissible: it records history rather than
   overwriting it.
2. Appends are serialised per chain. Two concurrent mutations cannot mint the
   same ``seq``, because that would silently fork the chain.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core.canonical import canonical_json
from ..core.hashchain import (
    GENESIS_HASH,
    ChainVerification,
    LedgerEntry,
    LedgerEvent,
    compute_entry_hash,
    merkle_root,
    sign_anchor,
    verify_entries,
)
from ..db.models import ChainAnchor, LedgerEntryORM

log = logging.getLogger(__name__)


class LedgerError(RuntimeError):
    pass


class AppendOnlyViolation(LedgerError):
    pass


def _to_domain(row: LedgerEntryORM) -> LedgerEntry:
    return LedgerEntry(
        id=row.id,
        seq=row.seq,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        chain_key=row.chain_key,
        event_type=row.event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        district_code=row.district_code,
        payload=row.payload or {},
        occurred_at=row.occurred_at,
        created_at=row.created_at,
    )


async def head(session: AsyncSession, chain_key: str) -> LedgerEntryORM | None:
    stmt = (select(LedgerEntryORM)
            .where(LedgerEntryORM.chain_key == chain_key)
            .order_by(LedgerEntryORM.seq.desc())
            .limit(1))
    return (await session.execute(stmt)).scalar_one_or_none()


async def append(
    session: AsyncSession,
    *,
    event_type: LedgerEvent | str,
    entity_type: str,
    entity_id: str,
    actor_id: str,
    payload: dict[str, Any],
    settings: Settings,
    actor_role: str = "SYSTEM",
    district_code: str | None = None,
    occurred_at: datetime | None = None,
) -> LedgerEntryORM:
    """Append one entry. Returns the flushed (not committed) ORM row.

    The caller owns the transaction. We only flush so that the caller can
    include ``ledger_seq`` in the same commit as the business row it describes —
    if the business write fails, the ledger entry rolls back with it, and the
    chain never claims something happened that did not.
    """
    chain_key = settings.chain_key
    event = event_type.value if isinstance(event_type, LedgerEvent) else str(event_type)
    occurred = occurred_at or datetime.now(timezone.utc)

    previous = await head(session, chain_key)
    seq = 1 if previous is None else previous.seq + 1
    prev_hash = previous.entry_hash if previous else GENESIS_HASH

    entry_hash = compute_entry_hash(
        seq=seq, prev_hash=prev_hash, chain_key=chain_key, event_type=event,
        entity_type=entity_type, entity_id=entity_id, actor_id=actor_id,
        occurred_at=occurred, payload=payload,
    )

    row = LedgerEntryORM(
        seq=seq, chain_key=chain_key, prev_hash=prev_hash, entry_hash=entry_hash,
        event_type=event, entity_type=entity_type, entity_id=str(entity_id),
        actor_id=actor_id, actor_role=actor_role, district_code=district_code,
        payload=payload, occurred_at=occurred, created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise LedgerError(
            f"append to chain {chain_key!r} at seq {seq} rejected by the database: "
            f"{type(exc.__context__ or exc).__name__}: {exc.__context__ or exc}"
        ) from exc
    return row


async def load_entries(
    session: AsyncSession,
    *,
    chain_key: str,
    from_seq: int | None = None,
    to_seq: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    district_code: str | None = None,
    limit: int = 10_000,
) -> list[LedgerEntry]:
    stmt = select(LedgerEntryORM).where(LedgerEntryORM.chain_key == chain_key)
    if from_seq is not None:
        stmt = stmt.where(LedgerEntryORM.seq >= from_seq)
    if to_seq is not None:
        stmt = stmt.where(LedgerEntryORM.seq <= to_seq)
    if entity_type:
        stmt = stmt.where(LedgerEntryORM.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(LedgerEntryORM.entity_id == entity_id)
    if district_code:
        stmt = stmt.where(LedgerEntryORM.district_code == district_code)
    stmt = stmt.order_by(LedgerEntryORM.seq.asc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_domain(r) for r in rows]


async def verify(
    session: AsyncSession,
    settings: Settings,
    *,
    chain_key: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    check_anchors: bool = True,
) -> ChainVerification:
    """Recompute the chain and report every divergence."""
    chain = chain_key or settings.chain_key
    entries = await load_entries(session, chain_key=chain, entity_type=entity_type,
                                 entity_id=entity_id)
    anchors: list[dict[str, Any]] = []
    if check_anchors:
        rows = (await session.execute(
            select(ChainAnchor).where(ChainAnchor.chain_key == chain)
            .order_by(ChainAnchor.to_seq.asc())
        )).scalars().all()
        anchors = [{
            "from_seq": a.from_seq, "to_seq": a.to_seq,
            "merkle_root": a.merkle_root, "entry_count": a.entry_count,
            "signature": a.signature,
        } for a in rows]
    return verify_entries(entries, chain_key=chain, anchors=anchors,
                          signing_key=settings.ledger_signing_key)


async def tamper_check_entity(
    session: AsyncSession, settings: Settings,
    entity_type: str, entity_id: str,
) -> ChainVerification:
    """Has *this* record's history been altered?"""
    return await verify(session, settings, entity_type=entity_type, entity_id=entity_id)


async def anchor(
    session: AsyncSession,
    settings: Settings,
    *,
    published_ref: str | None = None,
    signed_by: str = "Board of Revenue (automated)",
) -> ChainAnchor | None:
    """Freeze [last anchored seq + 1 .. head] into a signed Merkle checkpoint.

    Called by a scheduled job (and by ``POST /v1/ledger/anchors``). The point of
    the checkpoint is that the root gets *published* — Gazette notice, BoR
    website, newspaper, or a mirror outside the revenue department. Signing
    without publishing defends against nothing a DBA cannot undo.
    """
    chain = settings.chain_key
    last = (await session.execute(
        select(ChainAnchor).where(ChainAnchor.chain_key == chain)
        .order_by(ChainAnchor.to_seq.desc()).limit(1)
    )).scalar_one_or_none()
    from_seq = (last.to_seq + 1) if last else 1

    entries = await load_entries(session, chain_key=chain, from_seq=from_seq)
    if not entries:
        return None

    to_seq = entries[-1].seq
    root = merkle_root([e.entry_hash for e in entries])
    payload = canonical_json({
        "chain": chain, "from_seq": from_seq, "to_seq": to_seq,
        "merkle_root": root, "entry_count": len(entries),
    })
    signature = sign_anchor(payload, settings.ledger_signing_key) \
        if settings.anchor_signing_enabled else None

    rec = ChainAnchor(
        chain_key=chain, from_seq=from_seq, to_seq=to_seq, entry_count=len(entries),
        merkle_root=root, signature=signature, signed_by=signed_by,
        anchored_at=datetime.now(timezone.utc), published_ref=published_ref,
        published_at=datetime.now(timezone.utc) if published_ref else None,
    )
    session.add(rec)
    await session.flush()

    # The checkpoint is itself an event in the chain it summarises.
    await append(session, event_type=LedgerEvent.CHAIN_ANCHORED, entity_type="chain_anchor",
                 entity_id=f"{chain}:{from_seq}-{to_seq}", actor_id="system",
                 actor_role="SYSTEM", district_code=None,
                 payload={"chain": chain, "from_seq": from_seq, "to_seq": to_seq,
                          "merkle_root": root, "entry_count": len(entries),
                          "published_ref": published_ref},
                 settings=settings)
    return rec


async def should_anchor(session: AsyncSession, settings: Settings) -> bool:
    last = (await session.execute(
        select(ChainAnchor).where(ChainAnchor.chain_key == settings.chain_key)
        .order_by(ChainAnchor.to_seq.desc()).limit(1)
    )).scalar_one_or_none()
    from_seq = (last.to_seq + 1) if last else 1
    count = (await session.execute(
        select(func.count()).select_from(LedgerEntryORM)
        .where(LedgerEntryORM.chain_key == settings.chain_key)
        .where(LedgerEntryORM.seq >= from_seq)
    )).scalar_one()
    return count >= settings.anchor_every_n_entries


async def entity_history(
    session: AsyncSession, settings: Settings,
    entity_type: str, entity_id: str,
) -> list[dict[str, Any]]:
    entries = await load_entries(session, chain_key=settings.chain_key,
                                 entity_type=entity_type, entity_id=entity_id)
    return [{
        "seq": e.seq, "event_type": e.event_type, "actor_id": e.actor_id,
        "actor_role": e.actor_role, "occurred_at": e.occurred_at,
        "entry_hash": e.entry_hash, "prev_hash": e.prev_hash,
        "payload": e.payload,
    } for e in entries]


async def proof_for(session: AsyncSession, settings: Settings, seq: int) -> dict[str, Any] | None:
    """Merkle inclusion proof of one entry within its anchored range."""
    row = (await session.execute(
        select(LedgerEntryORM).where(LedgerEntryORM.chain_key == settings.chain_key)
        .where(LedgerEntryORM.seq == seq)
    )).scalar_one_or_none()
    if row is None:
        return None
    anc = (await session.execute(
        select(ChainAnchor)
        .where(ChainAnchor.chain_key == settings.chain_key)
        .where(ChainAnchor.from_seq <= seq)
        .where(ChainAnchor.to_seq >= seq)
        .order_by(ChainAnchor.from_seq.asc()).limit(1)
    )).scalar_one_or_none()

    from ..core.hashchain import merkle_proof

    result: dict[str, Any] = {"seq": seq, "entry_hash": row.entry_hash,
                              "anchored": anc is not None}
    if anc:
        entries = await load_entries(session, chain_key=settings.chain_key,
                                     from_seq=anc.from_seq, to_seq=anc.to_seq)
        hashes = [e.entry_hash for e in entries]
        idx = next(i for i, e in enumerate(entries) if e.seq == seq)
        result.update({
            "anchor": {"from_seq": anc.from_seq, "to_seq": anc.to_seq,
                       "merkle_root": anc.merkle_root, "signature": anc.signature,
                       "published_ref": anc.published_ref,
                       "anchored_at": anc.anchored_at},
            "proof": merkle_proof(hashes, idx),
        })
    return result


async def assert_append_only(session: AsyncSession) -> dict[str, Any]:
    """Actively test that the database refuses to mutate history.

    A migration that forgets the triggers leaves a ledger that *looks* fine for
    years. This check is on the health endpoint and in CI.
    """
    if session.bind is None:
        return {"append_only": False, "detail": "no database bound"}
    dialect = session.bind.dialect.name
    result: dict[str, Any] = {"dialect": dialect, "append_only": False, "detail": ""}

    probe = None
    try:
        newest = await head(session, "append-only-probe")
        if newest is None:
            from ..core.hashchain import compute_entry_hash
            h = compute_entry_hash(seq=1, prev_hash=GENESIS_HASH, chain_key="append-only-probe",
                                   event_type="PROBE", entity_type="probe", entity_id="probe",
                                   actor_id="healthcheck", occurred_at=datetime.now(timezone.utc),
                                   payload={"probe": True})
            newest = LedgerEntryORM(seq=1, chain_key="append-only-probe", prev_hash=GENESIS_HASH,
                                    entry_hash=h, event_type="PROBE", entity_type="probe",
                                    entity_id="probe", actor_id="healthcheck",
                                    actor_role="SYSTEM", payload={"probe": True},
                                    occurred_at=datetime.now(timezone.utc),
                                    created_at=datetime.now(timezone.utc))
            session.add(newest)
            await session.flush()
            probe = True

        baseline = newest.entry_hash
        newest.entry_hash = "f" * 64
        await session.flush()
        # If we got here, the database let us rewrite history.
        newest.entry_hash = baseline
        await session.flush()
        result["detail"] = ("UPDATE on audit_log succeeded — append-only enforcement "
                            "is NOT active. Apply db/migrations/002_append_only.sql.")
        return result
    except Exception as exc:
        result["append_only"] = True
        result["detail"] = f"database refused the UPDATE ({type(exc).__name__})"
        await session.rollback()
        return result
    finally:
        if probe:
            with suppress(Exception):
                await session.rollback()
