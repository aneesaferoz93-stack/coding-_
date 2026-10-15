"""Health, readiness and the append-only self-test."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...core.hashchain import DEFAULT_CHAIN, GENESIS_HASH
from ...db.geo import postgis_available
from ...db.session import get_session
from ...services import ledger

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness")
async def health() -> dict:
    return {"status": "ok", "service": "kp-land-integrity"}


@router.get("/ready", summary="Readiness + integrity self-test")
async def ready(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Readiness also proves that the database still refuses to rewrite history.

    A ledger whose append-only trigger was dropped by a bad migration still
    answers queries happily for years. This endpoint actively attempts an
    UPDATE and requires the database to reject it.
    """
    db_info = await ledger_health(session)
    append_only = await ledger.assert_append_only(session)
    chain = await ledger.verify(session, settings)
    return {
        "status": "ok" if chain.intact and append_only["append_only"] else "degraded",
        "database": db_info,
        "append_only": append_only,
        "chain": {
            "chain_key": chain.chain_key,
            "entries": chain.entries_checked,
            "intact": chain.intact,
            "head_hash": chain.head_hash or GENESIS_HASH,
            "findings": [f.__dict__ | {"kind": f.kind.value} for f in chain.findings],
        },
        "phase": settings.phase,
        "pilot_districts": settings.pilot_districts,
        "efard_mode": settings.efard_mode,
        "nadra_mode": settings.nadra_mode,
    }


async def ledger_health(session: AsyncSession) -> dict:
    dialect = session.bind.dialect.name if session.bind else "unknown"
    info: dict = {"dialect": dialect}
    try:
        await session.execute(text("SELECT 1"))
        info["reachable"] = True
    except Exception as exc:
        info["reachable"] = False
        info["error"] = str(exc)
    info["postgis"] = await postgis_available(session)
    return info


@router.get("/config", summary="Active policy and rule configuration")
async def config(settings: Settings = Depends(get_settings)) -> dict:
    """Everything a district needs to know about how decisions are made."""
    from ...core.decision import PHASE_POLICIES, Phase
    from ...core.fraud_rules import DEFAULT_CONFIG, RULE_REGISTRY
    from ...core.ground_trust import DEFAULT_TRUST_CONFIG

    policy = PHASE_POLICIES[Phase(settings.phase)]
    return {
        "service": settings.service_name,
        "env": settings.env,
        "phase": settings.phase,
        "phase_policy": {
            "label": policy.label_en,
            "red_decision": policy.red_decision.value,
            "amber_decision": policy.amber_decision.value,
            "binding": policy.binding,
            "ground_mandatory": policy.ground_mandatory,
            "escalation_role": policy.escalation_role,
            "description": policy.describe_en(),
        },
        "pilot_districts": settings.pilot_districts,
        "rules": sorted(RULE_REGISTRY),
        "rule_config": {
            "version": DEFAULT_CONFIG.version,
            "fingerprint": DEFAULT_CONFIG.fingerprint(),
            "rapid_remutation_window_days": DEFAULT_CONFIG.rapid_remutation_window_days,
            "rapid_remutation_max": DEFAULT_CONFIG.rapid_remutation_max,
            "undervaluation_red_pct": str(DEFAULT_CONFIG.undervaluation_red_pct),
            "undervaluation_amber_pct": str(DEFAULT_CONFIG.undervaluation_amber_pct),
            "seller_age_red": DEFAULT_CONFIG.seller_age_red,
            "poa_requires_biometric": DEFAULT_CONFIG.poa_requires_biometric,
            "witness_required": DEFAULT_CONFIG.witness_required,
        },
        "ground_trust": {
            "version": DEFAULT_TRUST_CONFIG.version,
            "max_accuracy_m": DEFAULT_TRUST_CONFIG.max_accuracy_m,
            "geofence_m": DEFAULT_TRUST_CONFIG.geofence_m,
            "offline_fail_h": DEFAULT_TRUST_CONFIG.offline_fail_h,
            "required_witnesses": DEFAULT_TRUST_CONFIG.required_witnesses,
        },
        "ledger": {
            "chain_key": settings.chain_key,
            "anchor_every_n_entries": settings.anchor_every_n_entries,
            "genesis": DEFAULT_CHAIN,
        },
        "integrations": {
            "efard_mode": settings.efard_mode,
            "efard_preissue_check": settings.efard_preissue_check,
            "nadra_mode": settings.nadra_mode,
        },
        "ussd_shortcode": settings.ussd_shortcode,
    }
