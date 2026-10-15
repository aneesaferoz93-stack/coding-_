# KP Land Record Integrity & Fraud Prevention System (KPLR-IFPS)

An **add-on integrity layer for the existing KP e-Fard portal and e-registry.**
It does not rebuild e-Fard, does not replace LRMIS, and does not hold title.
It answers three questions the existing system does not ask:

1. **Has this record been changed since it was written?** — append-only
   SHA-256 hash chain with published Merkle anchors.
2. **Does this transaction look like fraud?** — nine explainable rules over data
   the Revenue Department already holds.
3. **Did anyone actually go to the land?** — a Patwari PWA with GPS, camera-only
   photographs, two NADRA-verified witnesses, and offline sync, scored against
   twelve anti-spoof signals.

```
e-Fard portal ──▶ POST /v1/integrations/efard/clearance ──▶ CLEAR | HOLD | BLOCK
                                    │
                    ┌───────────────┴────────────────┐
                    │  rules  →  hash chain  →  PWA  │
                    └────────────────────────────────┘
```

---

## Why this exists

The KP e-Fard portal, launched 2 October 2026, solved *distribution*: a citizen
can now get a Fard from home, from a NADRA e-Sahulat centre, or through the
Dastak app, behind a biometric check and an electronic payment, with a QR code
for verification. What it does not do is ask whether the mutation behind that
Fard is real.

Punjab tried to solve that by making a new instrument — the Green Property
Certificate — mandatory and suspending Fard issuance under rule 36-A of the
PLRA Rules 1968. It began in Sahiwal on 1 May 2026, went province-wide on
1 July, and Fard issuance had to be restored in non-digitised areas on 11 July.
The scheme was suspended for six months in August 2026.

**This system takes the opposite approach in every respect**: it adds a safety
net and never removes the existing one, it rolls out on data-driven gates
rather than a calendar, and it seeks legal cover before enforcement, not after.
See [`docs/03-punjab-postmortem.md`](docs/03-punjab-postmortem.md).

---

## Quick start

```bash
# Backend
python -m venv .venv && ./.venv/bin/pip install -e "backend[dev]"
cd backend && ../.venv/bin/python -m pytest -q      # 123 tests

# Run the six-scene end-to-end demo (no credentials, no database needed)
cd backend && ../.venv/bin/python scripts/demo.py

# Patwari PWA
cd pwa && npm install && npm run dev
```

The demo runs on SQLite with a simulated e-Fard port and a simulated NADRA
port. It exercises: the full rule set, a ground verification that clears a
HOLD, six spoofing attempts caught by different signals, the Pashto USSD walk,
an insider tampering with the database, and Merkle anchor publication.

---

## What's in the box

| Deliverable | Where |
|---|---|
| **(a) Architecture — how it plugs into e-Fard** | [`docs/01-architecture.md`](docs/01-architecture.md) |
| **(b) DB schema: audit_log, fraud_flags, ground_verification** | [`db/migrations/001_schema.sql`](db/migrations/001_schema.sql) |
| **(c) Python: hash chain + fraud rule engine** | [`backend/app/core/`](backend/app/core/) |
| **(d) PWA: ground verification, anti-spoof GPS** | [`pwa/`](pwa/) |
| **(e) Rollout plan with Punjab failure analysis** | [`docs/02-rollout-plan.md`](docs/02-rollout-plan.md) · [`docs/03-punjab-postmortem.md`](docs/03-punjab-postmortem.md) |

Plus: [`docs/04-fraud-rules.md`](docs/04-fraud-rules.md) (the nine rules and
their limits), [`docs/05-threat-model.md`](docs/05-threat-model.md) (what this
system cannot do), and
[`docs/06-legal-amendment-draft.md`](docs/06-legal-amendment-draft.md) (Phase 3
drafting notes for the KP Land Revenue Act amendment).

---

## The core idea: integrity, not truth

> **A hash chain proves the record has not changed. It does not prove the
> record was ever right.**
>
> A false mutation entered on day one will be faithfully preserved by the
> chain. That is why the ledger is paired with ground verification, and why the
> two are documented together rather than sold separately.

This distinction is written into the Phase 3 legal draft
([§2.2](docs/06-legal-amendment-draft.md)), which creates a presumption of
*non-alteration* and explicitly preserves the defence that an entry was
inaccurate when first recorded.

---

## The five bugs worth knowing about before you read the code

1. **Canonicalisation is the hard part of hashing.** Two machines must produce
   the same bytes for the same record, or the alarm is noise.
   [`app/core/canonical.py`](backend/app/core/canonical.py) sorts keys, uses
   `Decimal` for money, and drives timestamps to UTC with fixed precision.
2. **A hash chain whose table accepts UPDATE is decoration.**
   [`db/migrations/002_append_only.sql`](db/migrations/002_append_only.sql)
   layers a trigger, a PostgreSQL rule, a `REVOKE`, and externally published
   Merkle roots — because each layer defeats a different attacker.
3. **Score the anti-spoof signals; do not gate on them.** In rural KP a weak
   GPS fix is usually terrain, not fraud, and a rule that punishes terrain gets
   switched off. See [`app/core/ground_trust.py`](backend/app/core/ground_trust.py).
4. **Fail open.** If this service is unreachable, e-Fard proceeds and we record
   a `DEGRADED` outcome. An anti-fraud layer that can take down revenue
   services is an anti-fraud layer that gets removed.
5. **Never store CNICs.** Salted hashes only. A land-records database is the
   wrong place for a province-wide list of national identity numbers.

---

## Honest limitations

Stated up front because a system whose limits are unstated gets trusted past
them:

* **A web browser cannot prove a GPS fix is real.** Android's
  `Location.isFromMockProvider()` is not exposed to web code. The PWA collects
  signals; the server scores them. A native Android wrapper with the Play
  Integrity API is the Phase 2+ recommendation.
* **We do not prove title.** That remains LRMIS / e-Fard and, legally, the
  manual register until Phase 3.
* **We do not fix historical defects.** No comprehensive settlement survey has
  been done in 125 years. We flag defect classes; a settlement programme fixes
  them.
* **A determined DBA can still rewrite the database.** We make it detectable
  within one anchoring interval and provable afterwards. That is the achievable
  goal.

Full register in [`docs/05-threat-model.md`](docs/05-threat-model.md).

---

## Accessibility

Pashto-first field app; Pashto / Urdu / English throughout. **USSD `*444#`**
for citizens without smartphones or data — status check, fraud report, language
switch, no internet required:

```
*444#  →  ۱ د ځمکې حالت   ۲ درغلي خبر کړئ   ۳ ژبه   ۴ مرسته
*444#1*512*MZ-PSH-001  →
   بند شوی. انتقال ودرول شو. سمدستي د ډپټي کمشنر دفتر سره اړیکه ونیسئ.
```

---

## Repository layout

```
backend/
  app/core/          canonical.py  hashchain.py  fraud_rules.py
                     decision.py   ground_trust.py  ownership.py
  app/services/      ledger.py  mutations.py  ground.py  ownership.py  ussd.py
  app/integrations/  efard.py  nadra.py     ← EfardPort: 5 methods, 2 impls
  app/api/routers/   mutations  fraud  ground  geo  ledger  dashboard  ussd
  tests/             123 tests
  scripts/demo.py    six-scene end-to-end demo
db/migrations/       001_schema.sql  002_append_only.sql  003_pilot_seed.sql
pwa/                 React 18 + TypeScript + Leaflet, offline-first
docs/                01 architecture · 02 rollout · 03 Punjab post-mortem
                     04 fraud rules · 05 threat model · 06 legal draft
```

---

## Configuration

Every behavioural switch is an environment variable. The two that matter most
for governance:

| Variable | Effect |
|---|---|
| `KLRI_ACTIVE_PHASE` | `PHASE_0_PILOT` … `PHASE_3_LEGAL`. Sets whether decisions bind. |
| `KLRI_PILOT_DISTRICTS` | Comma-separated district codes. Empty ⇒ pass-through CLEAR. |
| `KLRI_ENFORCEMENT_ENABLED` | `false` ⇒ **one-variable rollback**, recorded on the chain. |
| `KLRI_EFARD_MODE` | `simulated` (default, no credentials) or `http` + `KLRI_EFARD_BASE_URL`. |
| `KLRI_DATABASE_URL` | `sqlite+aiosqlite:///…` (default) or `postgresql+asyncpg://…`. |
| `KLRI_GEO_ENGINE` | `postgis` / `shapely` / `auto`. Reported in every response. |

See `backend/app/config.py` for the full list.

---

## Licence / status

**Pilot-grade software, not production-certified.** The seeded mouza boundaries
are rectangular bounding boxes and the DC rates are illustrative; both must be
replaced with SDLR outputs and the notified rate schedule before any real
pilot. The Phase 0 exit gates in
[`docs/02-rollout-plan.md`](docs/02-rollout-plan.md) are the checklist.
