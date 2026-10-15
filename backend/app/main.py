"""
KP Land Record Integrity & Fraud Prevention System — API entry point.

This service does not issue Fard. It sits beside the e-Fard portal / LRMIS and
answers one question the portal does not currently ask:

    "Is the mutation behind this document real?"

It answers with an opinion whose legal force depends on the rollout phase, and
it writes every question and every answer into an append-only hash chain so
that the opinion itself is auditable.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routers import (
    dashboard,
    fraud,
    geo,
    ground,
    health,
    integrations,
    mutations,
    ussd,
)
from .api.routers import (
    ledger as ledger_router,
)
from .config import get_settings
from .db.bootstrap import bootstrap
from .db.session import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("klri")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())

    if settings.env == "prod":
        # Fail loudly rather than shipping an open, demo-keyed service.
        if settings.ledger_signing_key.startswith("dev-only"):
            raise RuntimeError(
                "KLRI_LEDGER_SIGNING_KEY is still the development default. "
                "The ledger signature is the evidence a court relies on; it must "
                "be a real key held by the Board of Revenue.")
        if "dev-" in "".join(settings.service_api_keys.values()):
            raise RuntimeError("Development service API keys are still configured.")

    engine, sessionmaker = init_db(settings)
    info = await bootstrap(engine, sessionmaker, settings)
    app.state.settings = settings
    app.state.bootstrap = info
    log.info("started | env=%s phase=%s districts=%s db=%s append_only=%s",
             settings.env, settings.phase, ",".join(settings.pilot_districts),
             "sqlite" if settings.is_sqlite else "postgresql",
             info.get("append_only"))
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="KP Land Record Integrity & Fraud Prevention System",
        version="1.0.0",
        description=(
            "An anti-fraud and ground-verification layer that plugs into the "
            "existing KP e-Fard portal and e-registry. It does not issue land "
            "records; it checks the mutations behind them, detects tampering in "
            "the record, and verifies that a Patwari physically attended the "
            "parcel. Rolled out in phases so that no citizen loses access to a "
            "document they can obtain today."),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def timing(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Elapsed-Ms"] = f"{elapsed_ms:.1f}"
        # e-Fard calls us synchronously in its issuance path; if we are slow we
        # become the reason citizens queue. Make that visible on every response.
        if elapsed_ms > 1000:
            log.warning("slow request %s %s took %.0f ms",
                        request.method, request.url.path, elapsed_ms)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": "internal error",
            # Never leak internals, but do give the operator a correlation handle.
            "path": request.url.path,
        })

    prefix = settings.api_prefix
    app.include_router(health.router, prefix=prefix)
    app.include_router(integrations.router, prefix=prefix)
    app.include_router(ledger_router.router, prefix=prefix)
    app.include_router(mutations.router, prefix=prefix)
    app.include_router(fraud.router, prefix=prefix)
    app.include_router(ground.router, prefix=prefix)
    app.include_router(geo.router, prefix=prefix)
    app.include_router(ussd.router, prefix=prefix)
    app.include_router(dashboard.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "service": settings.service_name,
            "version": "1.0.0",
            "docs": "/docs",
            "phase": settings.phase,
            "pilot_districts": settings.pilot_districts,
            "ussd": settings.ussd_shortcode,
            "note": ("This is a verification layer on top of the existing e-Fard "
                     "portal. It does not issue Fard and does not replace LRMIS."),
        }

    return app


app = create_app()
