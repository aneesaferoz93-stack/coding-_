"""
bootstrap.py — schema creation, append-only enforcement, and pilot reference data.

Running this on a fresh SQLite file gives a complete working system, which
matters: a district should be able to pilot this on one server with no DBA, no
PostGIS and no network. Running it against PostgreSQL + PostGIS (after applying
db/migrations) gives the production system with real spatial indexing.

The append-only guarantee is installed here too, because a hash chain whose
table accepts UPDATE is decoration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from .geo import make_box_wkt
from .models import Base, DcRate, District, LathaRecord, Mouza, Officer, OfficerRole

log = logging.getLogger(__name__)

# Kept as a list of complete statements: splitting a SQL script on ";" breaks
# on the semicolons inside the RAISE() message strings.
SQLITE_APPEND_ONLY_TRIGGERS = [
    # audit_log must be append-only. If these triggers are missing, the entire
    # tamper-detection guarantee is void, so /ready checks for their effect by
    # attempting an UPDATE and requiring the database to refuse.
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT,
            'audit_log is append-only: UPDATE is forbidden. Write a new entry instead.');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT,
            'audit_log is append-only: DELETE is forbidden. Write a new entry instead.');
    END
    """,
    # chain_anchor is append-only for the same reason: an attacker who can
    # rewrite the published Merkle root has defeated the anchoring defence.
    """
    CREATE TRIGGER IF NOT EXISTS trg_chain_anchor_no_update
    BEFORE UPDATE ON chain_anchor
    BEGIN
        SELECT RAISE(ABORT, 'chain_anchor is append-only: UPDATE is forbidden.');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_chain_anchor_no_delete
    BEFORE DELETE ON chain_anchor
    BEGIN
        SELECT RAISE(ABORT, 'chain_anchor is append-only: DELETE is forbidden.');
    END
    """,
]

PG_APPEND_ONLY = """
-- See db/migrations/002_append_only.sql for the canonical PostgreSQL version
-- (which uses RULEs + a SECURITY DEFINER escrow function so that even the
-- table owner cannot rewrite history). This block is the SQLite equivalent and
-- is installed automatically by bootstrap().
"""


async def create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def install_append_only(session: AsyncSession) -> str:
    """Install the dialect-appropriate append-only enforcement."""
    dialect = session.bind.dialect.name if session.bind else "sqlite"
    if dialect == "sqlite":
        for stmt in SQLITE_APPEND_ONLY_TRIGGERS:
            await session.execute(text(stmt.strip()))
        await session.commit()
        return "sqlite-triggers"
    # PostgreSQL: the triggers/rules live in 002_append_only.sql because they
    # need to survive as a reviewed migration artefact, not as startup magic.
    return "postgres-migration-002"


# --------------------------------------------------------------------------
# Reference data for the pilot districts
# --------------------------------------------------------------------------

PILOT_DISTRICTS = [
    ("PESHAWAR", "Peshawar", "پشاور", "پېښور"),
    ("MARDAN", "Mardan", "مردان", "مردان"),
    ("SWAT", "Swat", "سوات", "سوات"),
]

# (mouza_code, district, tehsil, name_en, name_ur, name_ps, box, total_khasras,
#  latha_scanned_pct)
PILOT_MOUZAS = [
    ("MZ-PSH-001", "PESHAWAR", "PESHAWAR", "Landi Arbab", "لنڈی ارباب", "لنډي ارباب",
     (71.6000, 34.0200, 71.6500, 34.0700), 412, Decimal("88.0")),
    ("MZ-PSH-002", "PESHAWAR", "PESHAWAR", "Mattani", "متنی", "متني",
     (71.5600, 33.9300, 71.6100, 33.9800), 355, Decimal("74.0")),
    ("MZ-MRD-002", "MARDAN", "MARDAN", "Bakhshali", "بخشالی", "بخشالي",
     (71.9900, 34.2600, 72.0500, 34.3100), 508, Decimal("81.5")),
    ("MZ-MRD-003", "MARDAN", "TAKHT-I-BHAI", "Takht-i-Bhai", "تخت بھائی", "تخت بهايي",
     (71.9100, 34.2800, 71.9800, 34.3400), 631, Decimal("69.0")),
    ("MZ-SWT-001", "SWAT", "BABUZAI", "Saidu Sharif", "سیدو شریف", "سيدو شريف",
     (72.3900, 34.9600, 72.4600, 35.0200), 447, Decimal("92.0")),
    ("MZ-SWT-002", "SWAT", "MATTA", "Matta", "مٹہ", "مټه",
     (72.5800, 35.0200, 72.6600, 35.0900), 589, Decimal("63.0")),
]

# DC (Deputy Commissioner) rate table — the reference for Rule 2.
# Illustrative values, in PKR per kanal. A real deployment loads the notified
# rate schedule per district per financial year.
PILOT_DC_RATES = [
    ("PESHAWAR", None, "AGRICULTURAL", Decimal("1200000")),
    ("PESHAWAR", "MZ-PSH-001", "AGRICULTURAL", Decimal("1200000")),
    ("PESHAWAR", "MZ-PSH-002", "AGRICULTURAL", Decimal("1500000")),
    ("PESHAWAR", None, "RESIDENTIAL", Decimal("3500000")),
    ("MARDAN", None, "AGRICULTURAL", Decimal("700000")),
    ("MARDAN", "MZ-MRD-002", "AGRICULTURAL", Decimal("700000")),
    ("MARDAN", "MZ-MRD-003", "AGRICULTURAL", Decimal("850000")),
    ("SWAT", None, "AGRICULTURAL", Decimal("900000")),
    ("SWAT", "MZ-SWT-001", "AGRICULTURAL", Decimal("900000")),
    ("SWAT", "MZ-SWT-002", "AGRICULTURAL", Decimal("650000")),
]

# Scanned pre-computerisation register entries — the independent witness for
# Rule 5. In production these come from the latha scanning programme; the
# Directorate of Land Records already runs this for the Settlement &
# Digitization of Land Records (SDLR) programme.
PILOT_LATHA = [
    ("PESHAWAR", "MZ-PSH-001", "441", "12", "Gul Muhammad Khan", "Sher Dil Khan",
     "Khattak", "Landi Arbab", Decimal("8.0"), 2016, "PSH-001/p.112"),
    ("PESHAWAR", "MZ-PSH-002", "330", "91", "Nasreen Bibi", "Ghulam Nabi",
     "Mohmand", "Mattani", Decimal("12.0"), 2014, "PSH-002/p.207"),
    ("MARDAN", "MZ-MRD-002", "1902", "44", "Zarin Gul", "Amir Gul",
     "Yousafzai", "Bakhshali", Decimal("10.0"), 2018, "MRD-002/p.031"),
    # Khasra 77 still stands in the name of the deceased father — the mutation
    # was never entered after his death. That is the single most common state of
    # a KP inheritance record, and Rule 5 must handle it as "reconcile this",
    # not as "you are a fraudster".
    ("MARDAN", "MZ-MRD-003", "77", "23", "Rozi Khan", "Sherin Khan",
     "Yousafzai", "Takht-i-Bhai", Decimal("5.0"), 2012, "MRD-003/p.149"),
    ("SWAT", "MZ-SWT-001", "88", "7", "Mir Afzal Khan", "Bahram Khan",
     "Yousafzai", "Saidu Sharif", Decimal("6.0"), 2015, "SWT-001/p.058"),
    ("SWAT", "MZ-SWT-002", "1401", "55", "Sher Zaman", "Zaman Ali",
     "Yousafzai", "Matta", Decimal("20.0"), 2017, "SWT-002/p.402"),
]

PILOT_OFFICERS = [
    ("PAT-PSH-07", "Aurangzeb Khan", OfficerRole.PATWARI, "PESHAWAR", "PESHAWAR"),
    ("PAT-PSH-12", "Fida Muhammad", OfficerRole.PATWARI, "PESHAWAR", "PESHAWAR"),
    ("PAT-MRD-03", "Ihsanullah", OfficerRole.PATWARI, "MARDAN", "MARDAN"),
    ("PAT-MRD-09", "Nawab Ali", OfficerRole.PATWARI, "MARDAN", "TAKHT-I-BHAI"),
    ("PAT-SWT-04", "Zubair Ahmad", OfficerRole.PATWARI, "SWAT", "MATTA"),
    ("PAT-SWT-11", "Bakhtiar Ali", OfficerRole.PATWARI, "SWAT", "BABUZAI"),
    ("TEH-PSH-01", "Tehsildar Peshawar", OfficerRole.TEHSILDAR, "PESHAWAR", "PESHAWAR"),
]


# (mouza, khasra, owner_name, cnic, share_num, share_den, area_kanal)
# The share register is what makes double-sale detection (Rule 7) possible.
# MZ-PSH-001/512 is deliberately split: the seller in the demo corpus holds
# only half of it, so conveying "the whole" triggers R7.
PILOT_OWNERSHIP = [
    ("PESHAWAR", "MZ-PSH-001", "441", "Gul Muhammad Khan", "1730112345671", 1, 1, "8.0"),
    ("PESHAWAR", "MZ-PSH-001", "512", "Fazal Rahim", "1730155512345", 1, 2, "2.0"),
    ("PESHAWAR", "MZ-PSH-001", "512", "Haleema Bibi", "1730155512999", 1, 2, "2.0"),
    ("PESHAWAR", "MZ-PSH-002", "330", "Nasreen Bibi", "1730144455566", 1, 1, "12.0"),
    ("MARDAN", "MZ-MRD-002", "1902", "Zarin Gul", "1710177788899", 1, 1, "10.0"),
    ("MARDAN", "MZ-MRD-003", "77", "Bakht Zamina", "1710133344455", 1, 1, "5.0"),
    ("SWAT", "MZ-SWT-001", "88", "Mir Afzal Khan", "1720111122233", 1, 1, "6.0"),
    ("SWAT", "MZ-SWT-002", "1401", "Sher Zaman", "1720155566677", 1, 1, "20.0"),
]


async def seed_ownership(session: AsyncSession, settings) -> int:
    """Populate the khasra share register from the jamabandi.

    Without this table, Rule 7 (double sale) cannot function. It is the single
    most valuable data-preparation task for a pilot district, and it is also
    where decades of unrecorded inheritance show up as shares that do not sum
    to a whole — which is exactly what /v1/mutations/audit/oversubscribed is
    there to find.
    """
    from datetime import datetime, timezone

    from ..integrations.nadra import cnic_hash
    from .models import KhasraShare

    existing = (await session.execute(text("SELECT COUNT(*) FROM khasra_share"))).scalar_one()
    if existing:
        return 0

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    n = 0
    for (dist, mouza, khasra, name, cnic, num, den, area) in PILOT_OWNERSHIP:
        session.add(KhasraShare(
            khasra_key=f"{dist}/{mouza}/{khasra}",
            district_code=dist, mouza_code=mouza, khasra_no=khasra,
            owner_cnic_hash=cnic_hash(cnic, settings.ledger_signing_key),
            owner_name=name, share_num=num, share_den=den,
            area_kanal=Decimal(area), valid_from=now,
            source_mutation_id="SEED-JAMABANDI"))
        n += 1
    await session.commit()
    return n


async def seed_reference_data(session: AsyncSession, settings=None) -> dict[str, int]:
    """Idempotent: only inserts what is missing."""
    counts: dict[str, int] = {}

    existing = {r[0] for r in (await session.execute(
        text("SELECT code FROM district"))).all()}

    for code, name_en, name_ur, name_ps in PILOT_DISTRICTS:
        if code not in existing:
            session.add(District(code=code, name_en=name_en, name_ur=name_ur,
                                 name_ps=name_ps, live=True, e_registry_live=True))
            counts["district"] = counts.get("district", 0) + 1

    existing_mouza = {r[0] for r in (await session.execute(
        text("SELECT code FROM mouza"))).all()}
    for (code, dist, tehsil, name_en, name_ur, name_ps, box,
         total, scanned) in PILOT_MOUZAS:
        if code in existing_mouza:
            continue
        minlon, minlat, maxlon, maxlat = box
        session.add(Mouza(
            code=code, district_code=dist, tehsil_code=tehsil, name_en=name_en,
            name_ur=name_ur, name_ps=name_ps,
            boundary_wkt=make_box_wkt(minlon, minlat, maxlon, maxlat),
            centroid_lat=(minlat + maxlat) / 2, centroid_lon=(minlon + maxlon) / 2,
            digitised=True, latha_scanned_pct=scanned, total_khasras=total))
        counts["mouza"] = counts.get("mouza", 0) + 1

    n = (await session.execute(text("SELECT COUNT(*) FROM dc_rate"))).scalar_one()
    if not n:
        for dist, mouza, land_type, rate in PILOT_DC_RATES:
            session.add(DcRate(district_code=dist, mouza_code=mouza, land_type=land_type,
                               rate_per_kanal=rate,
                               effective_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
                               source_notification="Illustrative pilot rate schedule"))
            counts["dc_rate"] = counts.get("dc_rate", 0) + 1

    n = (await session.execute(text("SELECT COUNT(*) FROM latha_record"))).scalar_one()
    if not n:
        for (dist, mouza, khasra, khewat, owner, father, qaum, mouza_name,
             area, year, page) in PILOT_LATHA:
            session.add(LathaRecord(
                district_code=dist, mouza_code=mouza, khasra_no=khasra, khewat_no=khewat,
                owner_name=owner, father_name=father, qaum=qaum,
                mouza_name_in_record=mouza_name, area_kanal=area,
                jamabandi_year=year, scanned_page=page))
            counts["latha_record"] = counts.get("latha_record", 0) + 1

    for (oid, name, role, dist, tehsil) in PILOT_OFFICERS:
        if await session.get(Officer, oid) is None:
            session.add(Officer(id=oid, name=name, role=role, district_code=dist,
                                tehsil_code=tehsil))
            counts["officer"] = counts.get("officer", 0) + 1

    await session.commit()

    if settings is not None:
        counts["khasra_share"] = await seed_ownership(session, settings)

    return counts


async def bootstrap(engine, sessionmaker: async_sessionmaker[AsyncSession],
                    settings: Settings) -> dict:
    """Create schema, enforce append-only, seed reference data."""
    await create_schema(engine)
    info: dict = {}
    async with sessionmaker() as s:
        info["append_only"] = await install_append_only(s)
        try:
            info["seeded"] = await seed_reference_data(s, settings)
        except Exception as exc:
            log.warning("reference data seeding skipped: %s", exc)
            await s.rollback()
            info["seed_error"] = str(exc)
    return info
