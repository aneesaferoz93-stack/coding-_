"""seed_dev.py — populate the running dev database so the preview has data.

The PWA's assignment dropdown is empty on a fresh database, because assignments
are derived from mutations that have not yet been evaluated. This script seeds
the Phase 0 pilot reference data and runs the seven simulated e-Fard mutations
through the clearance endpoint, so the field app has something to work with.

    cd backend && ../.venv/bin/python scripts/seed_dev.py

Safe to re-run: reference data is upserted and mutations are re-evaluated, not
duplicated.

This is a developer convenience, NOT a production migration. For a real pilot
use db/migrations/003_pilot_seed.sql — reviewed SQL against the real district,
mouza and DC rate tables.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient  # noqa: E402


async def main() -> int:
    from app.config import get_settings
    from app.db.bootstrap import create_schema, install_append_only, seed_reference_data
    from app.db.session import build_engine, build_sessionmaker
    from app.main import create_app

    settings = get_settings()

    if not settings.is_sqlite:
        print("refusing: this script is for the local SQLite dev database.")
        print("for PostgreSQL, use db/migrations/*.sql")
        return 1

    engine = build_engine(settings)
    Session = build_sessionmaker(engine)
    await create_schema(engine)
    async with Session() as s:
        await install_append_only(s)
        counts = await seed_reference_data(s, settings)
    print(f"reference data: {counts}")

    # Point the app at this engine. The endpoints otherwise use their own
    # lazily-built engine, which is a different connection (or a different file).
    from app.db import session as db_session

    db_session._engine = engine
    db_session._sessionmaker = Session

    app = create_app()
    # Without this the endpoints use the cached default Settings, whose signing
    # key differs from the one that produced the seeded CNIC hashes — and then
    # every seller looks like they hold nothing.
    from app.config import get_settings as _get_settings

    app.dependency_overrides[_get_settings] = lambda: settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://dev") as client:
        rows = (await client.get("/v1/integrations/efard/demo/mutations")).json()
        for row in rows:
            r = await client.post(
                "/v1/integrations/efard/clearance",
                json={"mutation_id": row["mutation_id"], "channel": "PORTAL"},
            )
            if r.status_code >= 400:
                print(f"  ! {row['mutation_id']}: {r.status_code} {r.text[:200]}")
                continue
            body = r.json()
            print(f"  {row['mutation_id']:14s} {body['decision']:8s} "
                  f"score={body['score']:3d}  seq={body.get('ledger_seq')}")

        verify = (await client.get("/v1/ledger/verify")).json()

    await engine.dispose()

    print(f"\nseeded {len(rows)} mutations")
    print(f"ledger: {verify.get('entry_count')} entries, intact={verify.get('intact')}")
    print("open the Patwari PWA and pick an assignment from the dropdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
