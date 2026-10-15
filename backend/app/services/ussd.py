"""
ussd.py — the *444# fallback for citizens without smartphones.

KP's e-Fard is delivered through a website, the Dastak app and NADRA e-Sahulat
centres. All three assume a smartphone, data, or a trip to a franchise. For a
subsistence farmer in Kohistan or Upper Dir, none of those is a given — and
those are exactly the landowners whose holdings get stolen, because they cannot
check.

USSD needs no data connection, works on every GSM handset sold in Pakistan
since 2005, and costs nothing per session if the telco carries it on a short
code. So the *same* fraud status that a lawyer sees on a dashboard is available
as nine keypresses on a Rs 2,000 phone.

Two services are exposed:

  * Status check  — "is this khasra flagged?" (read-only, no login)
  * Fraud report  — "someone is selling my land" (creates a flag for follow-up)

Deliberately NOT exposed over USSD: anything that transfers or amends a
mutation. USSD has no display, no document, and no reliable identity — it is a
read-and-alert channel, never a transaction channel.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core.hashchain import LedgerEvent
from ..db.models import FlagStatus, FraudFlag, Mutation, MutationStatus, UssdSession
from ..i18n.messages import t
from ..integrations.nadra import cnic_hash, normalise_cnic
from . import ledger

log = logging.getLogger(__name__)

SESSION_TTL_MIN = 5
MAX_INPUT_LEN = 24


class MenuState(str, Enum):
    ROOT = "ROOT"
    STATUS_KHASRA = "STATUS_KHASRA"
    STATUS_MOUZA = "STATUS_MOUZA"
    REPORT_KHASRA = "REPORT_KHASRA"
    REPORT_CNIC = "REPORT_CNIC"
    HELP = "HELP"
    DONE = "DONE"


@dataclass(slots=True)
class UssdResponse:
    text: str
    continue_session: bool
    state: MenuState
    lang: str = "ps"

    def to_gateway(self, session_id: str, msisdn: str = "") -> dict[str, Any]:
        """Shape used by most Pakistani telco USSD gateways (Jazz/Telenor/Zong)."""
        return {
            "sessionId": session_id,
            "msisdn": msisdn,
            "message": self.text,
            "continueSession": self.continue_session,
        }


@dataclass
class UssdGateway:
    """Stateless USSD state machine, persisted per session."""

    session_id: str
    msisdn_hash: str
    lang: str = "ps"
    state: MenuState = MenuState.ROOT
    buffer: dict[str, Any] = field(default_factory=dict)
    last_input_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def expired(self) -> bool:
        return datetime.now(timezone.utc) - self.last_input_at > timedelta(minutes=SESSION_TTL_MIN)


def _hash_msisdn(msisdn: str, pepper: str) -> str:
    return hashlib.sha256(f"{pepper}:{normalise_cnic(msisdn)}".encode()).hexdigest()


async def load_gateway(session: AsyncSession, session_id: str, msisdn: str,
                       settings: Settings) -> UssdGateway:
    row = await session.get(UssdSession, session_id)
    if row is None:
        row = UssdSession(session_id=session_id,
                          msisdn_hash=_hash_msisdn(msisdn, settings.ledger_signing_key),
                          language="ps", menu_state=MenuState.ROOT.value, buffer={})
        session.add(row)
        await session.flush()
    return UssdGateway(session_id=row.session_id, msisdn_hash=row.msisdn_hash,
                       lang=row.language, state=MenuState(row.menu_state),
                       buffer=dict(row.buffer or {}), last_input_at=row.last_input_at)


async def save_gateway(session: AsyncSession, gw: UssdGateway) -> None:
    row = await session.get(UssdSession, gw.session_id)
    if row is None:
        return
    row.menu_state = gw.state.value
    row.buffer = gw.buffer
    row.language = gw.lang
    row.last_input_at = datetime.now(timezone.utc)
    row.closed = gw.state is MenuState.DONE
    await session.flush()


async def handle(
    session: AsyncSession,
    *,
    session_id: str,
    msisdn: str,
    text: str,
    settings: Settings,
) -> UssdResponse:
    """Process one USSD callback.

    Pakistani operators use the *accumulated input* model: every callback
    carries the entire path the user has typed so far ("" → "1" → "1*512" →
    "1*512*MZ-PSH-001"), not just the newest keypress. So we replay the whole
    path from ROOT on each call instead of trying to remember where we were.
    That makes the service stateless with respect to the gateway, which matters
    because USSD sessions are dropped and re-established constantly on 2G.
    """
    gw = await load_gateway(session, session_id, msisdn, settings)

    steps = [s for s in (text or "").split("*") if s != ""]
    state = MenuState.ROOT
    buffer: dict[str, Any] = {}
    lang = gw.lang
    resp = UssdResponse(t("menu.root", lang), True, MenuState.ROOT, lang)

    for i, step in enumerate(steps):
        last = i == len(steps) - 1

        if state is MenuState.ROOT:
            if step == "1":
                state = MenuState.STATUS_KHASRA
                resp = UssdResponse(t("menu.ask_khasra", lang), True, state, lang)
            elif step == "2":
                state = MenuState.REPORT_KHASRA
                resp = UssdResponse(t("menu.ask_khasra", lang), True, state, lang)
            elif step == "3":
                state = MenuState.HELP
                resp = UssdResponse(t("menu.language", lang), True, state, lang)
            elif step == "4":
                state = MenuState.DONE
                resp = UssdResponse(t("help.text", lang, code=settings.ussd_shortcode),
                                    False, state, lang)
            else:
                resp = UssdResponse(t("menu.root", lang), True, MenuState.ROOT, lang)

        elif state is MenuState.STATUS_KHASRA:
            buffer["khasra"] = step[:MAX_INPUT_LEN]
            state = MenuState.STATUS_MOUZA
            resp = UssdResponse(t("menu.ask_mouza", lang), True, state, lang)

        elif state is MenuState.STATUS_MOUZA:
            buffer["mouza"] = step[:MAX_INPUT_LEN].upper()
            if last:
                gw.buffer = buffer
                resp = await _status(session, gw, settings, lang)
                state = MenuState.DONE

        elif state is MenuState.REPORT_KHASRA:
            buffer["khasra"] = step[:MAX_INPUT_LEN]
            state = MenuState.REPORT_CNIC
            resp = UssdResponse(t("menu.ask_cnic", lang), True, state, lang)

        elif state is MenuState.REPORT_CNIC:
            buffer["cnic"] = normalise_cnic(step)
            if last:
                gw.buffer = buffer
                resp = await _report(session, gw, settings, lang)
                state = MenuState.DONE

        elif state is MenuState.HELP:
            # Language submenu
            lang = {"1": "ps", "2": "ur", "3": "en"}.get(step, lang)
            gw.lang = lang
            state = MenuState.DONE
            resp = UssdResponse(t("menu.language_set", lang, code=settings.ussd_shortcode),
                                False, state, lang)

    gw.state = state
    gw.buffer = {**gw.buffer, **buffer}

    await ledger.append(
        session,
        event_type=LedgerEvent.OFFICER_ACTION,
        entity_type="ussd_session",
        entity_id=session_id,
        actor_id=f"msisdn:{gw.msisdn_hash[:12]}",
        actor_role="CITIZEN",
        payload={"state": gw.state.value, "language": lang,
                 "buffer_keys": sorted(gw.buffer.keys()),
                 "channel": "USSD", "shortcode": settings.ussd_shortcode},
        settings=settings,
    )
    await save_gateway(session, gw)
    return resp


async def _status(session: AsyncSession, gw: UssdGateway, settings: Settings,
                  lang: str) -> UssdResponse:
    khasra = str(gw.buffer.get("khasra", ""))
    mouza = str(gw.buffer.get("mouza", "")).upper()

    stmt = (select(Mutation)
            .where(Mutation.khasra_no == khasra)
            .where(Mutation.mouza_code == mouza)
            .order_by(Mutation.proposed_at.desc()).limit(1))
    mut = (await session.execute(stmt)).scalar_one_or_none()

    flags = (await session.execute(
        select(FraudFlag)
        .where(FraudFlag.khasra_no == khasra)
        .where(FraudFlag.mouza_code == mouza)
        .where(FraudFlag.severity == "RED")
        .where(FraudFlag.status.in_([FlagStatus.OPEN, FlagStatus.UNDER_REVIEW]))
        .order_by(FraudFlag.raised_at.desc()).limit(3)
    )).scalars().all()

    if mut is None and not flags:
        return UssdResponse(t("status.unknown", lang), False, MenuState.DONE, lang)

    if flags:
        from ..i18n.messages import rule_label
        what = ", ".join(dict.fromkeys(rule_label(f.rule_id, lang) for f in flags))
        body = t("status.blocked", lang)
        return UssdResponse(f"{body}\n{what}", False, MenuState.DONE, lang)

    if mut is not None and mut.status in (MutationStatus.HELD, MutationStatus.BLOCKED):
        return UssdResponse(t("status.held", lang), False, MenuState.DONE, lang)

    return UssdResponse(t("status.clear", lang), False, MenuState.DONE, lang)


async def _report(session: AsyncSession, gw: UssdGateway, settings: Settings,
                  lang: str) -> UssdResponse:
    """A citizen says 'someone is transferring my land'.

    We do not verify the claim — we record it, put it in the chain, and give the
    citizen a reference. The flag attaches to the khasra so the next clearance
    request on that parcel surfaces it.
    """
    khasra = str(gw.buffer.get("khasra", ""))
    cnic = str(gw.buffer.get("cnic", ""))
    ref = f"RPT-{datetime.now(timezone.utc).strftime('%y%m%d')}-{gw.session_id[-6:].upper()}"

    session.add(FraudFlag(
        mutation_id=None,
        district_code="",
        mouza_code=str(gw.buffer.get("mouza", "")).upper(),
        khasra_no=khasra,
        rule_id="CITIZEN",
        rule_name="Citizen report via USSD",
        severity="AMBER",
        title=f"Citizen report {ref}",
        detail=("Landowner reported a suspected unauthorised transfer through "
                f"{settings.ussd_shortcode}. Must be checked against the mutation "
                "register before any further transfer on this khasra."),
        evidence={"reference": ref, "channel": "USSD",
                  "reporter_cnic_hash": cnic_hash(cnic, settings.ledger_signing_key)
                  if cnic else None},
        recommended_action="Tehsildar to contact the reporter and re-verify the khasra.",
        status=FlagStatus.OPEN,
        engine_version="ussd-1",
    ))
    await session.flush()

    await ledger.append(
        session,
        event_type=LedgerEvent.FRAUD_FLAG_RAISED,
        entity_type="citizen_report",
        entity_id=ref,
        actor_id=f"msisdn:{gw.msisdn_hash[:12]}",
        actor_role="CITIZEN",
        payload={"khasra_no": khasra, "mouza": gw.buffer.get("mouza"),
                 "reference": ref, "channel": "USSD"},
        settings=settings,
    )

    return UssdResponse(t("report.received", lang, ref=ref), False, MenuState.DONE, lang)
