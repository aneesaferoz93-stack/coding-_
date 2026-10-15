"""Async SQLAlchemy engine/session factory, portable across PostGIS and SQLite."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings


def build_engine(settings: Settings):
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["pool_pre_ping"] = True

    engine = create_async_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:
        # SQLite is not Postgres, but a ledger that silently ignores foreign keys
        # is not a ledger. Turn on the constraints we rely on.
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")   # a ledger must survive power loss
            cur.close()

    return engine


def build_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


_engine = None
_sessionmaker = None


def init_db(settings: Settings):
    global _engine, _sessionmaker
    _engine = build_engine(settings)
    _sessionmaker = build_sessionmaker(_engine)
    return _engine, _sessionmaker


def get_engine():
    if _engine is None:
        raise RuntimeError("database not initialised — call init_db(settings) first")
    return _engine


def get_sessionmaker():
    if _sessionmaker is None:
        raise RuntimeError("database not initialised — call init_db(settings) first")
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def healthcheck(session: AsyncSession) -> dict[str, Any]:
    """Report the dialect and whether PostGIS is actually available."""
    dialect = session.bind.dialect.name if session.bind else "unknown"
    info: dict[str, Any] = {"dialect": dialect, "postgis": False, "postgis_version": None}
    if dialect == "postgresql":
        try:
            row = (await session.execute(
                text("SELECT extname, extversion FROM pg_extension WHERE extname='postgis'")
            )).first()
            if row:
                info["postgis"] = True
                info["postgis_version"] = row[1]
        except Exception as exc:  # pragma: no cover
            info["error"] = str(exc)
    return info
