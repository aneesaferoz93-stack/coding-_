"""Configuration. Every value has a safe local default; nothing is secret-by-default."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_prefix="KLRI_", extra="ignore",
        env_file_encoding="utf-8",
    )

    # ---- service -----------------------------------------------------------
    service_name: str = "KP Land Record Integrity & Fraud Prevention System"
    env: Literal["dev", "staging", "prod"] = "dev"
    api_prefix: str = "/v1"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # ---- database ----------------------------------------------------------
    # Postgres + PostGIS is the production target. SQLite is supported so the
    # system can be demonstrated, tested and piloted on a laptop or a district
    # server with no DBA. PostGIS-only features degrade gracefully.
    database_url: str = "sqlite+aiosqlite:///./klri_dev.db"
    db_echo: bool = False
    db_pool_size: int = 10

    # ---- rollout -----------------------------------------------------------
    # Which gate is active. See docs/02-rollout-plan.md. In dev this is Phase 1
    # (dual-run): the system advises, it does not refuse.
    phase: Literal["PHASE_0_PILOT", "PHASE_1_DUAL_RUN", "PHASE_2_ENFORCE",
                   "PHASE_3_LEGAL"] = "PHASE_1_DUAL_RUN"

    # Districts where the layer is live. Everyone else passes through untouched,
    # which is the entire point of a phased rollout.
    pilot_districts: list[str] = Field(default_factory=lambda: ["PESHAWAR", "MARDAN", "SWAT"])

    # ---- ledger ------------------------------------------------------------
    chain_key: str = "kp-main"
    # Demo key. In production this is an HSM-held key owned by the Board of
    # Revenue, rotated on a published schedule.
    ledger_signing_key: str = "dev-only-insecure-key-change-before-any-pilot"
    anchor_every_n_entries: int = 50
    anchor_signing_enabled: bool = True

    # ---- e-Fard integration ------------------------------------------------
    # We do not replace e-Fard; we call it. Leave the URL unset to run against
    # the in-repo simulator (integrations/efard.py::SimulatedEfardPort).
    efard_base_url: str = ""
    efard_api_key: str = ""
    efard_verify_token: str = ""
    efard_timeout_s: float = 8.0
    efard_mode: Literal["simulated", "http"] = "simulated"
    # When True, e-Fard must call us before issuing; when False we only mirror
    # what was issued (post-hoc audit). See docs/01-architecture.md.
    efard_preissue_check: bool = True

    # ---- NADRA -------------------------------------------------------------
    nadra_base_url: str = ""
    nadra_api_key: str = ""
    nadra_mode: Literal["simulated", "http"] = "simulated"

    # ---- ground verification ----------------------------------------------
    ground_trust_accept_min: int = 50       # below this a visit does not count
    photo_max_bytes: int = 12 * 1024 * 1024
    device_token_secret: str = "dev-only-device-secret"
    device_token_ttl_h: int = 24 * 30

    # ---- security ----------------------------------------------------------
    # Service-to-service API keys (e-Fard, USSD gateway, dashboards).
    service_api_keys: dict[str, str] = Field(default_factory=lambda: {
        "efard-bridge": "dev-efard-key",
        "ussd-gateway": "dev-ussd-key",
        "dashboard": "dev-dashboard-key",
    })

    # ---- i18n ---------------------------------------------------------------
    default_language: Literal["en", "ur", "ps"] = "en"
    ussd_shortcode: str = "*444#"

    # ---- CORS ---------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def postgis_enabled(self) -> bool:
        return self.database_url.startswith("postgresql") and not self.is_sqlite


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("KLRI_ENV_FILE", ".env"))
