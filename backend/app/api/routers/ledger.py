"""Ledger read APIs: verify the chain, read history, publish anchors."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...core.hashchain import verify_merkle_proof
from ...db.session import get_session
from ...services import ledger
from ..deps import Principal, Role, require
from ..schemas import AnchorCreate, AnchorOut, ChainVerificationOut, LedgerEntryOut

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("/verify", response_model=ChainVerificationOut,
            summary="Recompute the hash chain and report tampering")
async def verify_chain(
    chain_key: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.AUDITOR)),
) -> ChainVerificationOut:
    """The single most important endpoint in this system.

    Recomputes every hash from the stored payloads and reports:
      HASH_MISMATCH  a record's contents were edited after writing
      BROKEN_LINK    the chain was spliced
      GAP            rows were deleted
      TRUNCATED      the tail was removed below a published anchor
      ANCHOR_MISMATCH the published Merkle root no longer matches
    """
    result = await ledger.verify(session, settings, chain_key=chain_key,
                                 entity_type=entity_type, entity_id=entity_id)
    return ChainVerificationOut(
        chain_key=result.chain_key,
        entries_checked=result.entries_checked,
        intact=result.intact,
        first_seq=result.first_seq,
        last_seq=result.last_seq,
        head_hash=result.head_hash,
        recomputed_root=result.recomputed_root,
        checked_at=result.checked_at,
        findings=[
            {"kind": f.kind.value, "seq": f.seq, "detail": f.detail,
             "expected": f.expected, "found": f.found}
            for f in result.findings
        ],
    )


@router.get("/entries", response_model=list[LedgerEntryOut],
            summary="Read ledger entries")
async def entries(
    from_seq: int = Query(1, ge=1),
    to_seq: int | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    district_code: str | None = None,
    event_type: str | None = None,
    limit: int = Query(200, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.AUDITOR)),
):
    rows = await ledger.load_entries(
        session, chain_key=settings.chain_key, from_seq=from_seq, to_seq=to_seq,
        entity_type=entity_type, entity_id=entity_id, district_code=district_code,
        limit=limit)
    if event_type:
        rows = [r for r in rows if r.event_type == event_type]
    return [LedgerEntryOut(
        seq=r.seq, chain_key=r.chain_key, prev_hash=r.prev_hash, entry_hash=r.entry_hash,
        event_type=r.event_type, entity_type=r.entity_type, entity_id=r.entity_id,
        actor_id=r.actor_id, actor_role=r.actor_role, district_code=r.district_code,
        payload=r.payload, occurred_at=r.occurred_at, created_at=r.created_at,
    ) for r in rows]


@router.get("/history/{entity_type}/{entity_id}",
            summary="Append-only history of one record (tamper checked)")
async def history(
    entity_type: str,
    entity_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.PATWARI)),
):
    entries = await ledger.entity_history(session, settings, entity_type, entity_id)
    check = await ledger.tamper_check_entity(session, settings, entity_type, entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "intact": check.intact,
        "findings": [{"kind": f.kind.value, "seq": f.seq, "detail": f.detail}
                     for f in check.findings],
        "entries": entries,
    }


@router.get("/entry/{seq}/proof", summary="Merkle inclusion proof for one entry")
async def proof(
    seq: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.AUDITOR)),
):
    result = await ledger.proof_for(session, settings, seq)
    if result is None:
        raise HTTPException(404, f"no ledger entry at seq {seq}")
    if result.get("proof"):
        result["proof_verified"] = verify_merkle_proof(
            result["entry_hash"], result["proof"], result["anchor"]["merkle_root"])
    return result


@router.post("/anchors", response_model=AnchorOut,
             summary="Freeze the chain into a signed, publishable checkpoint")
async def create_anchor(
    body: AnchorCreate,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require(Role.DEPUTY_COMMISSIONER)),
):
    """Signing an anchor is easy. Publishing it is what makes it worth anything.

    If you set ``published_ref`` (Gazette notice number, BoR URL, newspaper
    page) the root becomes independently verifiable — an attacker who rewrites
    the database still cannot rewrite every published copy.
    """
    rec = await ledger.anchor(session, settings, published_ref=body.published_ref,
                              signed_by=principal.subject)
    if rec is None:
        raise HTTPException(409, "nothing new to anchor")
    await session.commit()
    return AnchorOut.model_validate(rec)


@router.get("/anchors", response_model=list[AnchorOut], summary="List anchors")
async def list_anchors(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(require(Role.AUDITOR)),
):
    from sqlalchemy import select

    from ...core.canonical import canonical_json
    from ...core.hashchain import verify_anchor_signature
    from ...db.models import ChainAnchor

    rows = (await session.execute(
        select(ChainAnchor).where(ChainAnchor.chain_key == settings.chain_key)
        .order_by(ChainAnchor.to_seq.desc())
    )).scalars().all()
    out = []
    for a in rows:
        d = AnchorOut.model_validate(a)
        if a.signature:
            payload = canonical_json({
                "chain": a.chain_key, "from_seq": a.from_seq, "to_seq": a.to_seq,
                "merkle_root": a.merkle_root, "entry_count": a.entry_count})
            d = d.model_copy(update={
                "signature": a.signature,
                "signature_valid": verify_anchor_signature(
                    payload, a.signature, settings.ledger_signing_key),
            })
        out.append(d)
    return out
