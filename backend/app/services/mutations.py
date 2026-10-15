"""
mutations.py — the orchestration layer: e-Fard data → rule engine → ledger.

Nothing here makes a legal decision on its own. It assembles evidence, runs the
rules, writes an immutable record of what it saw and what it concluded, and
returns an opinion. The opinion's *force* comes from the active rollout phase
(core/decision.py), not from this module.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core.decision import Phase, decide
from ..core.fraud_rules import (
    DEFAULT_CONFIG,
    EvaluationResult,
    GroundEvidence,
    LathaRecord,
    MutationContext,
    NadraStatus,
    Party,
    RuleConfig,
    TransactionType,
    evaluate,
    mask_cnic,
)
from ..core.hashchain import LedgerEvent
from ..db.models import (
    Clearance,
    DcRate,
    FraudFlag,
    GroundVerification,
    Mutation,
    MutationStatus,
    OfficerRole,
)
from ..db.models import (
    LathaRecord as LathaORM,
)
from ..integrations.efard import EfardMutation, EfardPort
from ..integrations.nadra import NadraPort, VerificationOutcome, cnic_checksum_ok, cnic_hash
from . import ledger, ownership

log = logging.getLogger(__name__)

_NADRA_MAP = {
    VerificationOutcome.LIVE: NadraStatus.LIVE,
    VerificationOutcome.DECEASED: NadraStatus.DECEASED,
    VerificationOutcome.NOT_FOUND: NadraStatus.NOT_FOUND,
    VerificationOutcome.BLOCKED: NadraStatus.BLOCKED,
    VerificationOutcome.UNVERIFIED: NadraStatus.UNVERIFIED,
}


async def _resolve_parties(parties: list[dict[str, Any]], nadra: NadraPort,
                           settings: Settings) -> list[Party]:
    """Enrich parties with a live NADRA check.

    A failed or slow NADRA call degrades to UNVERIFIED, which the engine treats
    as AMBER for a human to resolve. It must never degrade to LIVE.
    """
    out: list[Party] = []
    for data in parties:
        cnic = data.get("cnic")
        status = NadraStatus.UNVERIFIED
        age = data.get("age")
        if cnic and cnic_checksum_ok(str(cnic)):
            try:
                res = await nadra.verify_party(str(cnic), data.get("name"),
                                               data.get("father_name"))
                status = _NADRA_MAP.get(res.outcome, NadraStatus.UNVERIFIED)
                if age in (None, ""):
                    age = res.age
            except Exception as exc:
                log.warning("NADRA lookup failed for %s: %s", mask_cnic(str(cnic)), exc)
                status = NadraStatus.UNVERIFIED
        elif cnic:
            status = NadraStatus.NOT_FOUND

        share = ownership.parse_share(str(data["share"])) if data.get("share") else None
        out.append(Party(
            name=str(data.get("name") or "UNKNOWN"),
            cnic=(str(cnic) if cnic else None),
            father_name=(str(data["father_name"]) if data.get("father_name") else None),
            qaum=(str(data["qaum"]) if data.get("qaum") else None),
            age=(int(age) if age not in (None, "") else None),
            nadra_status=status,
            biometric_verified=bool(data.get("biometric_verified")),
            acting_via_poa=bool(data.get("acting_via_poa")),
            poa_biometric_verified=bool(data.get("poa_biometric_verified")),
            poa_attorney_cnic=(str(data["poa_attorney_cnic"])
                               if data.get("poa_attorney_cnic") else None),
            poa_registered=bool(data.get("poa_registered")),
            resident_mouza=(str(data["resident_mouza"])
                            if data.get("resident_mouza") else None),
            share_numerator=(share[0] if share else None),
            share_denominator=(share[1] if share else None),
        ))
    return out


async def _dc_rate(session: AsyncSession, district: str, mouza: str) -> Decimal | None:
    now = datetime.now(timezone.utc)
    stmt = (select(DcRate.rate_per_kanal)
            .where(DcRate.district_code == district.upper())
            .where(DcRate.mouza_code == mouza)
            .where(DcRate.effective_from <= now)
            .where((DcRate.effective_to.is_(None)) | (DcRate.effective_to >= now))
            .order_by(DcRate.effective_from.desc()).limit(1))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    # Fall back to the district-wide rate; flagged in the payload if used.
    stmt2 = (select(DcRate.rate_per_kanal)
             .where(DcRate.district_code == district.upper())
             .where(DcRate.mouza_code.is_(None))
             .order_by(DcRate.effective_from.desc()).limit(1))
    return (await session.execute(stmt2)).scalar_one_or_none()


async def _latha(session: AsyncSession, district: str, mouza: str,
                 khasra: str) -> LathaRecord | None:
    stmt = (select(LathaORM)
            .where(LathaORM.district_code == district.upper())
            .where(LathaORM.mouza_code == mouza)
            .where(LathaORM.khasra_no == khasra)
            .order_by(LathaORM.jamabandi_year.desc()).limit(1))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return LathaRecord(
        owner_name=row.owner_name, father_name=row.father_name, qaum=row.qaum,
        mouza=row.mouza_name_in_record, khasra_no=row.khasra_no,
        area_kanal=row.area_kanal, jamabandi_year=row.jamabandi_year,
        scanned_page=row.scanned_page,
    )


async def _latest_ground(session: AsyncSession, mutation_id: str) -> GroundEvidence | None:
    stmt = (select(GroundVerification)
            .where(GroundVerification.mutation_id == mutation_id)
            .where(GroundVerification.status == "ACCEPTED")
            .order_by(GroundVerification.captured_at.desc()).limit(1))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return GroundEvidence(
        verified=True,
        latitude=row.latitude, longitude=row.longitude, accuracy_m=row.accuracy_m,
        captured_at=row.captured_at, synced_at=row.received_at,
        inside_mouza=row.inside_mouza,
        distance_to_boundary_m=row.distance_to_boundary_m,
        photo_sha256=row.photo_sha256, photo_exif_datetime=row.photo_exif_datetime,
        photo_from_camera=(row.photo_source == "camera"),
        witness_cnics=tuple(row.witness_cnic_hashes or []),
        witnesses_biometric=tuple(row.witness_nadra_ok or []),
        device_id=row.device_id,
        mock_location_suspected=bool(row.is_mock),
        trust_score=row.trust_score,
    )


async def build_context(
    session: AsyncSession,
    efard: EfardMutation,
    *,
    nadra: NadraPort,
    settings: Settings,
    efard_port: EfardPort | None = None,
    ground: GroundEvidence | None = None,
) -> MutationContext:
    """Assemble everything the rules need from systems that already exist."""
    district, mouza, khasra = efard.district_code, efard.mouza_code, efard.khasra_no

    sellers = tuple(await _resolve_parties(efard.sellers, nadra, settings))
    buyers = tuple(await _resolve_parties(efard.buyers, nadra, settings))

    dc_rate = await _dc_rate(session, district, mouza)
    latha = await _latha(session, district, mouza, khasra)

    # Rule 1 input: prior mutations on this khasra, from e-Fard itself.
    prior: list[datetime] = []
    if efard_port is not None:
        try:
            history = await efard_port.fetch_khasra_history(
                district, mouza, khasra, efard.proposed_at - timedelta(days=120))
            prior = [m.proposed_at for m in history if m.mutation_id != efard.mutation_id]
        except Exception as exc:
            log.warning("khasra history lookup failed: %s", exc)
    local = (await session.execute(
        select(Mutation.proposed_at)
        .where(Mutation.district_code == district.upper())
        .where(Mutation.mouza_code == mouza)
        .where(Mutation.khasra_no == khasra)
        .where(Mutation.id != efard.mutation_id)
        .where(Mutation.proposed_at >= efard.proposed_at - timedelta(days=120))
    )).scalars().all()
    prior.extend(local)
    prior = sorted(set(prior))

    # Rule 7 inputs.
    # If the share register has no rows for this khasra we report UNKNOWN, not
    # zero. Concluding "the seller owns nothing" from an empty table would flag
    # every unpopulated mouza in the province as a double sale — and an
    # anti-fraud rule that cries wolf on data gaps is a rule that gets muted.
    snap = await ownership.snapshot(session, district, mouza, khasra)
    shares_total = snap.total_share if snap.owners else None
    seller_held: Decimal | None = None
    if snap.owners and sellers and sellers[0].cnic:
        h = cnic_hash(sellers[0].cnic, settings.ledger_signing_key)
        seller_held = await ownership.held_share(session, district, mouza, khasra, h)

    # Rule 8 input.
    day_start = efard.proposed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    count_today = (await session.execute(
        select(func.count()).select_from(Mutation)
        .where(Mutation.patwari_id == efard.patwari_id)
        .where(Mutation.proposed_at >= day_start)
    )).scalar_one() if efard.patwari_id else 0

    if ground is None:
        ground = await _latest_ground(session, efard.mutation_id)

    try:
        txn = TransactionType(str(efard.transaction_type).upper())
    except ValueError:
        txn = TransactionType.SALE

    return MutationContext(
        mutation_id=efard.mutation_id,
        district_code=district.upper(),
        tehsil_code=efard.tehsil_code,
        mouza_code=mouza,
        mouza_name=efard.mouza_name,
        khasra_no=khasra,
        khewat_no=efard.khewat_no,
        area_kanal=efard.area_kanal,
        transaction_type=txn,
        declared_price_pkr=efard.declared_price_pkr,
        dc_rate_per_kanal=dc_rate,
        registered_at=efard.proposed_at,
        patwari_id=efard.patwari_id,
        patwari_mutations_today=int(count_today),
        sellers=sellers,
        buyers=buyers,
        prior_mutations=tuple(prior),
        seller_held_share=seller_held,
        khasra_shares_total=shares_total,
        latha=latha,
        ground=ground,
        extras={"efard_ref": getattr(efard, "raw", {}).get("efard_ref")},
    )


def _mirror_party(p: Party, settings: Settings) -> dict:
    """Non-sensitive mirror of a party. CNIC is stored only as a salted hash."""
    return {
        "name": p.name,
        "father_name": p.father_name,
        "qaum": p.qaum,
        "age": p.age,
        "nadra_status": p.nadra_status.value,
        "biometric_verified": p.biometric_verified,
        "acting_via_poa": p.acting_via_poa,
        "poa_biometric_verified": p.poa_biometric_verified,
        "poa_registered": p.poa_registered,
        "cnic_hash": cnic_hash(p.cnic, settings.ledger_signing_key) if p.cnic else None,
        "share": (f"{p.share_numerator}/{p.share_denominator}"
                  if p.share_numerator and p.share_denominator else None),
    }


def _persist_mutation(session: AsyncSession, efard: EfardMutation,
                      ctx: MutationContext, settings: Settings) -> Mutation:
    return Mutation(
        id=efard.mutation_id,
        efard_ref=(efard.raw.get("efard_ref") if efard.raw else None),
        district_code=ctx.district_code,
        tehsil_code=ctx.tehsil_code,
        mouza_code=ctx.mouza_code,
        mouza_name=ctx.mouza_name,
        khewat_no=ctx.khewat_no,
        khasra_no=ctx.khasra_no,
        area_kanal=ctx.area_kanal,
        transaction_type=ctx.transaction_type.value,
        declared_price_pkr=ctx.declared_price_pkr,
        dc_value_pkr=ctx.dc_value_pkr,
        patwari_id=ctx.patwari_id,
        patwari_mutations_today=ctx.patwari_mutations_today,
        seller_held_share=ctx.seller_held_share,
        status=MutationStatus.DRAFT,
        proposed_at=efard.proposed_at,
        payload={
            "area_kanal": str(ctx.area_kanal),
            "dc_rate_per_kanal": (str(ctx.dc_rate_per_kanal)
                                  if ctx.dc_rate_per_kanal else None),
            # Party attributes are mirrored WITHOUT the CNIC: we keep a salted
            # hash for linkage and re-derive anything sensitive from NADRA or
            # e-Fard at evaluation time. A land-record database is exactly the
            # kind of place a CNIC list should never sit.
            "sellers": [_mirror_party(p, settings) for p in ctx.sellers],
            "buyers": [_mirror_party(p, settings) for p in ctx.buyers],
        },
    )


async def run_clearance(
    session: AsyncSession,
    efard: EfardMutation,
    *,
    settings: Settings,
    nadra: NadraPort,
    efard_port: EfardPort | None = None,
    config: RuleConfig = DEFAULT_CONFIG,
    phase: Phase | None = None,
) -> tuple[EvaluationResult, Clearance, Mutation]:
    """Full pipeline: evidence → rules → decision → ledger → persisted flags."""
    ctx = await build_context(session, efard, nadra=nadra, settings=settings,
                              efard_port=efard_port)
    evaluation = evaluate(ctx, config)

    active_phase = phase or Phase(settings.phase)
    clearance_id = f"CLR-{efard.mutation_id}-" \
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    decision = decide(evaluation, phase=active_phase, clearance_id=clearance_id,
                      base_url=settings.public_base_url)

    # --- persist the mutation mirror -------------------------------------
    existing = await session.get(Mutation, efard.mutation_id)
    if existing is None:
        mut = _persist_mutation(session, efard, ctx, settings)
        session.add(mut)
        await session.flush()
    else:
        mut = existing
        mut.dc_value_pkr = ctx.dc_value_pkr
        mut.seller_held_share = ctx.seller_held_share
        mut.patwari_mutations_today = ctx.patwari_mutations_today
        mut.proposed_at = efard.proposed_at

    # --- ledger: what we saw ----------------------------------------------
    entry = await ledger.append(
        session,
        event_type=LedgerEvent.MUTATION_DRAFTED,
        entity_type="mutation",
        entity_id=efard.mutation_id,
        actor_id=efard.patwari_id or "unknown",
        actor_role=OfficerRole.PATWARI.value,
        district_code=ctx.district_code,
        payload={
            "district": ctx.district_code, "tehsil": ctx.tehsil_code,
            "mouza": ctx.mouza_code, "khewat_no": ctx.khewat_no,
            "khasra_no": ctx.khasra_no, "area_kanal": str(ctx.area_kanal),
            "transaction_type": ctx.transaction_type.value,
            "declared_price_pkr": str(ctx.declared_price_pkr),
            "dc_value_pkr": (str(ctx.dc_value_pkr) if ctx.dc_value_pkr else None),
            "sellers": [{"name_masked": p.name,
                         "cnic_masked": mask_cnic(p.cnic),
                         "nadra_status": p.nadra_status.value,
                         "age": p.age,
                         "biometric_verified": p.biometric_verified,
                         "acting_via_poa": p.acting_via_poa,
                         "poa_biometric_verified": p.poa_biometric_verified}
                        for p in ctx.sellers],
            "buyers": [{"name_masked": p.name, "cnic_masked": mask_cnic(p.cnic),
                        "nadra_status": p.nadra_status.value}
                       for p in ctx.buyers],
            "prior_mutations": [d.isoformat() for d in ctx.prior_mutations],
            "seller_held_share": (str(ctx.seller_held_share)
                                  if ctx.seller_held_share is not None else None),
            "khasra_shares_total": (str(ctx.khasra_shares_total)
                                    if ctx.khasra_shares_total is not None else None),
            "ground_trust_score": (ctx.ground.trust_score if ctx.ground else None),
        },
        settings=settings,
        occurred_at=efard.proposed_at,
    )

    # --- ledger: what we concluded ----------------------------------------
    decision_entry = await ledger.append(
        session,
        event_type=(LedgerEvent.MUTATION_BLOCKED if decision.decision.value == "BLOCK"
                    else LedgerEvent.MUTATION_HELD if decision.decision.value == "HOLD"
                    else LedgerEvent.MUTATION_CLEARED),
        entity_type="clearance",
        entity_id=clearance_id,
        actor_id="system",
        actor_role=OfficerRole.SYSTEM.value,
        district_code=ctx.district_code,
        payload={
            "mutation_id": efard.mutation_id,
            # Back-reference to the observation this decision was made on, so a
            # reviewer can walk from conclusion to evidence without trusting
            # either row's timestamp.
            "observed_seq": entry.seq,
            "observed_hash": entry.entry_hash,
            "decision": decision.decision.value,
            "phase": decision.phase.value,
            "binding": decision.binding,
            "score": decision.score,
            "severity": decision.severity.value,
            "reasons": decision.reasons,
            "engine_version": evaluation.engine_version,
            "config_fingerprint": evaluation.config_fingerprint,
            "findings": [f.to_dict() for f in evaluation.findings],
        },
        settings=settings,
    )

    decision.ledger_seq = decision_entry.seq
    decision.ledger_hash = decision_entry.entry_hash

    # --- persist flags ------------------------------------------------------
    for f in evaluation.findings:
        session.add(FraudFlag(
            mutation_id=efard.mutation_id,
            district_code=ctx.district_code,
            mouza_code=ctx.mouza_code,
            khasra_no=ctx.khasra_no,
            rule_id=f.rule_id,
            rule_name=f.rule_name,
            severity=f.severity.value,
            title=f.title,
            detail=f.detail,
            evidence=f.evidence,
            recommended_action=f.recommended_action,
            legal_basis=f.legal_basis,
            engine_version=evaluation.engine_version,
            config_fingerprint=evaluation.config_fingerprint,
        ))

    # --- persist the clearance ---------------------------------------------
    mut.status = {
        "CLEAR": MutationStatus.CLEARED,
        "HOLD": MutationStatus.HELD,
        "BLOCK": MutationStatus.BLOCKED,
        "ADVISORY": MutationStatus.CLEARED,
    }.get(decision.decision.value, MutationStatus.DRAFT)
    mut.decided_at = decision.decided_at

    rec = Clearance(
        clearance_id=clearance_id,
        mutation_id=efard.mutation_id,
        decision=decision.decision.value,
        phase=decision.phase.value,
        binding=decision.binding,
        severity=decision.severity.value,
        score=decision.score,
        reasons=decision.reasons,
        required_actions=[dataclasses.asdict(a) for a in decision.required_actions],
        escalation_role=decision.escalation_role,
        ledger_seq=decision.ledger_seq,
        ledger_hash=decision.ledger_hash,
        config_fingerprint=decision.config_fingerprint,
        decided_at=decision.decided_at,
        expires_at=decision.expires_at,
    )
    session.add(rec)
    await session.flush()

    return evaluation, rec, mut


async def request_clearance(
    session: AsyncSession,
    mutation_id: str,
    *,
    settings: Settings,
    nadra: NadraPort,
    efard_port: EfardPort,
    config: RuleConfig = DEFAULT_CONFIG,
) -> tuple[EvaluationResult, Clearance, Mutation]:
    """Entry point used by the e-Fard bridge endpoint."""
    efard = await efard_port.fetch_mutation(mutation_id)
    if efard is None:
        raise KeyError(mutation_id)
    return await run_clearance(session, efard, settings=settings, nadra=nadra,
                               efard_port=efard_port, config=config)
