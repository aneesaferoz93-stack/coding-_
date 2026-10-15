"""
ownership.py — share arithmetic and double-sale detection.

A khasra is not owned by one person; it is owned in fractions (``hissa``) that
descend through inheritance for generations. Fraud in this system is therefore
mostly *arithmetic*: someone conveys 1/2 when they hold 1/4, or two owners each
sell the whole, or shares quietly sum to more than the parcel.

We keep a materialised current-share table (``khasra_share``) derived from the
ledger, and we treat the ledger as the authority: if the two disagree, the
chain wins and the table is rebuilt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import KhasraShare

log = logging.getLogger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")


def khasra_key(district: str, mouza: str, khasra: str) -> str:
    return f"{district.upper()}/{mouza}/{khasra}"


@dataclass(slots=True)
class ShareSnapshot:
    khasra_key: str
    total_share: Decimal
    owners: list[dict[str, Any]]
    oversubscribed: bool
    ledger_consistent: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "khasra_key": self.khasra_key,
            "total_share": str(self.total_share),
            "owners": self.owners,
            "oversubscribed": self.oversubscribed,
        }


async def current_shares(session: AsyncSession, key: str) -> list[KhasraShare]:
    stmt = (select(KhasraShare)
            .where(KhasraShare.khasra_key == key)
            .where(KhasraShare.valid_to.is_(None))
            .order_by(KhasraShare.id.asc()))
    return list((await session.execute(stmt)).scalars().all())


async def snapshot(session: AsyncSession, district: str, mouza: str,
                   khasra: str) -> ShareSnapshot:
    key = khasra_key(district, mouza, khasra)
    rows = await current_shares(session, key)
    owners = [{
        "owner_cnic_hash": r.owner_cnic_hash,
        "owner_name": r.owner_name,
        "share": f"{r.share_num}/{r.share_den}",
        "share_decimal": str(Decimal(r.share_num) / Decimal(r.share_den)),
        "area_kanal": str(r.area_kanal) if r.area_kanal is not None else None,
        "valid_from": r.valid_from,
        "source_mutation_id": r.source_mutation_id,
    } for r in rows]
    total = sum((Decimal(r["share_decimal"]) for r in owners), ZERO)
    return ShareSnapshot(khasra_key=key,
                         total_share=total.quantize(Decimal("0.00000001")),
                         owners=owners,
                         oversubscribed=total > ONE + Decimal("0.0001"))


async def held_share(session: AsyncSession, district: str, mouza: str, khasra: str,
                     owner_cnic_hash: str) -> Decimal:
    rows = await current_shares(session, khasra_key(district, mouza, khasra))
    total = ZERO
    for r in rows:
        if r.owner_cnic_hash == owner_cnic_hash:
            total += Decimal(r.share_num) / Decimal(r.share_den)
    return total.quantize(Decimal("0.00000001"))


async def transfer_share(
    session: AsyncSession,
    *,
    district: str, mouza: str, khasra: str,
    from_hash: str, to_hash: str, to_name: str | None,
    share_num: int, share_den: int,
    mutation_id: str, ledger_seq: int | None,
    area_kanal: Decimal | None = None,
    at: datetime | None = None,
) -> tuple[KhasraShare, KhasraShare | None]:
    """Move ``share_num/share_den`` from one owner to another.

    If the transfer would oversubscribe the khasra we still record it — because
    the whole point is to make the inconsistency visible and attributable — but
    we log it loudly and the caller surfaces a Rule 7 flag.
    """
    when = at or datetime.now(timezone.utc)
    key = khasra_key(district, mouza, khasra)
    rows = await current_shares(session, key)

    outgoing: KhasraShare | None = None
    for r in rows:
        if r.owner_cnic_hash == from_hash:
            outgoing = r
            break

    created: KhasraShare | None = None
    if outgoing is not None:
        outgoing.valid_to = when
        remaining = (Decimal(outgoing.share_num) / Decimal(outgoing.share_den)
                     - Decimal(share_num) / Decimal(share_den))
        if remaining > ZERO:
            # Seller keeps a residue — write a new current row for what's left.
            session.add(KhasraShare(
                khasra_key=key, district_code=district.upper(), mouza_code=mouza,
                khasra_no=khasra, owner_cnic_hash=from_hash,
                owner_name=outgoing.owner_name,
                share_num=int(remaining * Decimal(outgoing.share_den)),
                share_den=outgoing.share_den,
                area_kanal=(outgoing.area_kanal - area_kanal) if (
                    outgoing.area_kanal is not None and area_kanal is not None) else None,
                valid_from=when, source_mutation_id=mutation_id, ledger_seq=ledger_seq,
            ))

    created = KhasraShare(
        khasra_key=key, district_code=district.upper(), mouza_code=mouza,
        khasra_no=khasra, owner_cnic_hash=to_hash, owner_name=to_name,
        share_num=share_num, share_den=share_den, area_kanal=area_kanal,
        valid_from=when, source_mutation_id=mutation_id, ledger_seq=ledger_seq,
    )
    session.add(created)
    await session.flush()

    snap = await snapshot(session, district, mouza, khasra)
    if snap.oversubscribed:
        log.error("OVERSUBSCRIPTION on %s after mutation %s: total=%s (seq=%s)",
                  key, mutation_id, snap.total_share, ledger_seq)
    return created, outgoing


async def oversubscribed_khasras(session: AsyncSession, *,
                                 district: str | None = None,
                                 limit: int = 200) -> list[dict[str, Any]]:
    """Every khasra whose recorded shares exceed a whole.

    This is the query a district runs on day one of a pilot: it is the list of
    parcels already broken before the anti-fraud layer arrived.
    """
    stmt = (select(
        KhasraShare.khasra_key,
        KhasraShare.district_code,
        KhasraShare.mouza_code,
        KhasraShare.khasra_no,
        func.sum(KhasraShare.share_num.cast(db_numeric()) /
                 KhasraShare.share_den.cast(db_numeric())).label("total"),
    ).where(KhasraShare.valid_to.is_(None)))
    if district:
        stmt = stmt.where(KhasraShare.district_code == district.upper())
    stmt = stmt.group_by(KhasraShare.khasra_key, KhasraShare.district_code,
                         KhasraShare.mouza_code, KhasraShare.khasra_no)
    rows = (await session.execute(stmt)).all()
    bad = [r for r in rows if r[4] is not None and Decimal(str(r[4])) > ONE]
    bad.sort(key=lambda r: Decimal(str(r[4])), reverse=True)
    return [{
        "khasra_key": r[0], "district_code": r[1], "mouza_code": r[2],
        "khasra_no": r[3], "total_share": str(Decimal(str(r[4])).quantize(Decimal("0.0001"))),
    } for r in bad[:limit]]


def db_numeric():  # pragma: no cover - dialect shim
    from sqlalchemy import Numeric
    return Numeric(20, 10)


def parse_share(text: str) -> tuple[int, int] | None:
    """Parse '1/4' or '0.25' into (num, den)."""
    if not text:
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            return int(num.strip()), int(den.strip())
        except ValueError:
            return None
    try:
        dec = Decimal(text)
    except Exception:
        return None
    # Common revenue fractions.
    for den in (2, 4, 8, 16, 32, 64, 3, 6, 5, 10, 20, 100):
        num = dec * den
        if num == num.to_integral_value():
            return int(num), den
    return int(dec * 1000), 1000
