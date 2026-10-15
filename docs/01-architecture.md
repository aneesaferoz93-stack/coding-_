# Architecture — how this plugs into e-Fard instead of replacing it

## 0. One-paragraph summary

The KP e-Fard portal (launched 2 October 2026) already does the hard part of
*distribution*: it lets a citizen get a Fard from home, from a NADRA e-Sahulat
centre, or through the Dastak app, behind a NADRA biometric check and an
electronic payment, with a QR code for verification [3](https://propakistani.pk/2026/10/03/kp-launches-e-fard-portal-for-easy-access-to-land-services/).
What it does not do is ask whether the mutation behind that Fard is real.

This system is a **filter and a tape recorder** that e-Fard calls. It does not
hold title, does not issue documents, does not replace LRMIS, and does not
change who is entitled to what. It adds three capabilities on top:

| Capability | What it does | Where it lives |
|---|---|---|
| **Mutation integrity** | Every mutation and every Fard is written to an append-only SHA-256 hash chain, so any later edit is detectable and attributable | `backend/app/core/hashchain.py`, `db/migrations/002_append_only.sql` |
| **Fraud rules** | Nine explainable rules over data the Revenue Department already holds | `backend/app/core/fraud_rules.py` |
| **Ground verification** | A Patwari must physically attend the parcel; the evidence is scored against twelve anti-spoof signals | `backend/app/core/ground_trust.py`, `pwa/` |

---

## 1. The boundary (what we own, what we do not)

```
                        ┌───────────────────────────────────────────┐
                        │  BOARD OF REVENUE — SYSTEM OF RECORD      │
                        │                                           │
                        │   LRMIS  ──  e-Fard portal  ──  e-registry│
                        │   (title, mutation, Fard issuance)        │
                        │   Already has: NADRA biometrics, QR,      │
                        │   1LINK / Easypaisa payment, PSID         │
                        └───────────────────┬───────────────────────┘
                                            │
                        ════════════════════╪═══════════════════════
                              WE DO NOT     │     CROSS THIS LINE
                        ════════════════════╪═══════════════════════
                                            │
                                  ┌─────────▼──────────┐
                                  │  EfardPort         │  ← the whole contract
                                  │  (Protocol, 5      │     is 5 methods
                                  │   methods)         │
                                  └─────────┬──────────┘
                                            │
   ┌────────────────────────────────────────▼──────────────────────────────┐
   │        KP LAND RECORD INTEGRITY & FRAUD PREVENTION SYSTEM             │
   │                                                                       │
   │  ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌─────────┐ │
   │  │ Clearance   │──▶│ Rule engine  │──▶│ Hash chain   │──▶│ Anchors │ │
   │  │ API         │   │ R1…R9        │   │ (append-only)│   │ Merkle  │ │
   │  └─────────────┘   └──────┬───────┘   └──────┬───────┘   └────┬────┘ │
   │         │                 │                  │                 │      │
   │         │          ┌──────▼───────┐   ┌──────▼───────┐        │      │
   │         │          │ DC rate table│   │ Ownership    │        │      │
   │         │          │ (Rule 2)     │   │ shares (R7)  │        │      │
   │         │          └──────────────┘   └──────────────┘        │      │
   │         │                 │                                    │      │
   │         │          ┌──────▼────────┐                  publish  │      │
   │         │          │ NADRA verif.  │                    ▼      │      │
   │         │          │ (Rule 3)      │              Gazette / BoR│      │
   │         │          └───────────────┘                 website   │      │
   │         │                                                      │      │
   │  ┌──────▼──────────────────────────────────────────────┐       │      │
   │  │ Ground verification: Patwari PWA → trust scoring     │───────┘      │
   │  │ GPS + camera + 2 witnesses, offline-first            │              │
   │  └─────────────────────────────────────────────────────┘              │
   │         │                                                             │
   │  ┌──────▼─────────┐                                                   │
   │  │ USSD *444#     │  Pashto / Urdu / English, no smartphone           │
   │  └────────────────┘                                                   │
   └───────────────────────────────────────────────────────────────────────┘
```

**The rule we never break:** this system never answers "who owns this land?"
from its own data. The answer always comes from e-Fard/LRMIS. We answer "is the
record behind this document internally consistent, corroborated on the ground,
and unchanged since it was written?"

---

## 2. Integration Mode A — pre-issue interception (prevention)

e-Fard already runs a synchronous pipeline before it prints a Fard: NADRA
biometric → payment (1LINK/Easypaisa) → issue. We add **one more call in that
same sequence**.

```
  Citizen ──▶ e-Fard portal ──▶ NADRA biometric ──▶ payment ──▶ ISSUE Fard
                    │                                              ▲
                    └──▶ POST /v1/integrations/efard/clearance ────┘
                              returns CLEAR | HOLD | BLOCK
```

Contract:

```http
POST /v1/integrations/efard/clearance
X-API-Key: <issued to the BoR integration>
Content-Type: application/json

{ "mutation_id": "MUT-KP-1002", "channel": "PORTAL" }
```

```json
{
  "clearance_id": "CLR-MUT-KP-1002-20261015...",
  "decision": "HOLD",
  "phase": "PHASE_1_DUAL_RUN",
  "binding": false,
  "severity": "RED",
  "score": 100,
  "reasons": ["R1 [RED] Khasra 512 mutated 2x in 30 days", "..."],
  "required_actions": [
    {"action": "Escalate to Tehsildar...", "owner_role": "Tehsildar", "sla_hours": 24}
  ],
  "ledger_seq": 4,
  "ledger_hash": "0133dca147d96605...",
  "verify_url": "https://.../v1/integrations/efard/verify/CLR-...",
  "findings": [ { "rule_id": "R1", "severity": "RED", "title": "...", "detail": "..." } ]
}
```

Two properties matter:

1. **It is fast.** Rules run on data already in memory or in a single indexed
   query. Budget: under 300 ms p99. If we are slow, we become the reason
   citizens queue, and the integration will be switched off — correctly.
2. **It fails open, deliberately.** If this service is unreachable, e-Fard
   proceeds and we record a `DEGRADED` outcome for retrospective re-checking.

### Why fail-open is the right call

A fail-closed design would make our uptime a precondition for issuing any land
record in the province. That is the exact shape of the failure Punjab hit: when
the Green Property Certificate became the sole valid instrument, the
alternative system could not serve every case on day one, Fard issuance had to
be restored in non-digitised areas within weeks [5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/),
and the Punjab Assembly suspended the scheme for six months in August
2026 [1](https://propakistani.pk/2026/08/28/punjab-suspends-digital-land-record-certificates-for-6-months/).

An anti-fraud layer that can take down revenue services is an anti-fraud layer
that gets removed. Ours cannot.

---

## 3. Integration Mode B — post-issue mirroring (retrospective)

For districts where the pre-issue call is not yet wired in, or for anything
already attested, e-Fard tells us what it issued and we write it to the chain:

```http
POST /v1/integrations/efard/mirror
{ "fard_id": "FARD-…", "district_code": "PESHAWAR", "mouza_code": "MZ-PSH-001",
  "khasra_no": "441", "channel": "DASTAK", "document_sha256": "…" }
```

This is what makes Phase 0 worth running at all: even before enforcement, a
district can answer *"what exactly did we issue last month, and has any of it
been touched since?"*

---

## 4. The whole integration surface

That is it. Five methods:

```python
class EfardPort(Protocol):
    async def fetch_mutation(self, mutation_id) -> EfardMutation | None: ...
    async def fetch_khasra_history(self, district, mouza, khasra, since) -> [...]: ...
    async def fetch_recent_fards(self, since, district=None) -> [...]: ...
    async def verify_qr(self, qr_payload) -> dict | None: ...
    async def notify_decision(self, clearance) -> bool: ...
```

Two implementations ship:

* `HttpEfardPort` — production. Base URL, API key, optional mTLS from
  environment. No code changes to point at staging.
* `SimulatedEfardPort` — a deterministic corpus of seven mutations covering
  every rule. Runs with zero credentials, which is why the pilot can be
  demonstrated to a Deputy Commissioner on a laptop.

---

## 5. Data flow for one mutation

```
 1. e-Fard calls /v1/integrations/efard/clearance
 2. We fetch the mutation (or accept it pushed in the request body)
 3. District not in the pilot? → CLEAR, pass-through, nothing recorded.
 4. Enrich from systems that already exist:
      • DC rate table          → Rule 2 reference value
      • NADRA                  → life status, age, biometric (Rule 3)
      • khasra_share           → what the seller actually holds (Rule 7)
      • latha (scanned manual) → independent identity witness (Rule 5)
      • mutation history       → re-mutation velocity (Rule 1)
      • ground_verification    → was anyone on site? (Rule 6)
 5. Run rules R1–R9 → findings with severity + evidence + recommended action
 6. Apply the ACTIVE PHASE policy → CLEAR / HOLD / BLOCK / ADVISORY
 7. Write two ledger entries: what we saw, and what we concluded
 8. Persist one row per finding into fraud_flags
 9. Return the decision, the reasons, and the ledger hash
10. Optionally push the decision back to e-Fard (notify_decision)
```

Steps 7 and 8 are not logging. They are the product: a Patwari who is accused
of forgery two years later can point at a chain entry and prove what the record
said on the day.

---

## 6. Component map

| Path | Responsibility |
|---|---|
| `backend/app/core/canonical.py` | Deterministic serialisation. Without it, two machines hash the same record differently and the alarm becomes noise. |
| `backend/app/core/hashchain.py` | Entry hashing, Merkle roots, verification, anchor signing. Pure functions — runnable by a court-appointed auditor on a database copy they do not trust. |
| `backend/app/core/fraud_rules.py` | The nine rules. No ML in the decision path: every finding carries the numbers that produced it. |
| `backend/app/core/decision.py` | Maps severity × rollout phase → decision. The file where the Punjab lesson is encoded. |
| `backend/app/core/ground_trust.py` | Twelve anti-spoof signals, scored not gated. |
| `backend/app/services/ledger.py` | The append path. |
| `backend/app/services/mutations.py` | Evidence assembly → rules → decision → persistence. |
| `backend/app/services/ownership.py` | Share arithmetic and double-sale detection. |
| `backend/app/integrations/` | `EfardPort` and `NadraPort`, each with an HTTP and a simulated implementation. |
| `backend/app/api/routers/` | REST surface under `/v1`. |
| `pwa/` | React + Leaflet PWA for the Patwari. Offline-first. |
| `db/migrations/` | PostgreSQL + PostGIS schema, append-only enforcement, pilot seed. |

---

## 7. Deployment topologies

**Single district pilot (works today, no DBA):**
```
uvicorn app.main:app --host 0.0.0.0 --port 8000     # SQLite, shapely geofence
cd pwa && npm run dev                                # PWA on the same box
```
Geofencing falls back from PostGIS to shapely automatically, and every API
response reports which engine was used. A silent fallback would be a security
bug, so there is no silent fallback.

**Province-scale:**
```
docker compose up -d      # api (N workers) + postgis + optional read replica
```
Set `KLRI_DATABASE_URL=postgresql+asyncpg://…` and PostGIS spatial indexes,
`ST_Contains` and `ST_Distance` switch on automatically.

---

## 8. Performance budget

| Operation | Budget | Notes |
|---|---|---|
| Pre-issue clearance | < 300 ms p99 | Inline in e-Fard's issuance path |
| Ledger append | < 15 ms | One indexed read + one insert |
| Chain verify (10k entries) | < 2 s | Recomputes every hash; run off-peak, and per-record on demand |
| Ground visit ingest | < 200 ms | Includes geofence + duplicate-photo scan |
| USSD callback | < 400 ms | Telcos time out aggressively |

Anchoring (Merkle checkpoint every N entries, default 50) runs as a scheduled
job, never inline.
