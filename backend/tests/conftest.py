"""Shared fixtures: a fresh in-memory SQLite database per test."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.bootstrap import create_schema, install_append_only, seed_reference_data


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "phase": "PHASE_1_DUAL_RUN",
        "pilot_districts": ["PESHAWAR", "MARDAN", "SWAT"],
        "ledger_signing_key": "test-signing-key",
        "env": "dev",
    }
    base.update(overrides)
    return Settings(**base)


@pytest_asyncio.fixture
async def db():
    settings = make_settings()
    engine = create_async_engine(settings.database_url)
    await create_schema(engine)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        await install_append_only(s)
        await seed_reference_data(s)
    yield Session
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return make_settings()
