# Phased Rollout Plan

**Principle: we add a safety net; we never take away the existing one until the
safety net has been proven to hold weight.** For the evidence behind each gate,
see [03-punjab-postmortem.md](03-punjab-postmortem.md).

```
Phase 0   shadow        3 districts, advisory only, nothing stops
Phase 1   dual-run      manual register still legally valid, we flag
Phase 2   enforcement   binding HOLD/BLOCK in districts that passed the gates
Phase 3   legal cover   KP Revenue Act amendment; hash-verified record admissible
```

Each phase has **exit criteria measured in data**, plus an explicit **rollback
trigger** and a named **owner**. A phase that cannot be rolled back will not be
started.

---

## Phase 0 — Shadow (6 months)

**Districts: Peshawar, Mardan, Swat only.** Not all fourteen.

### Scope
* Integrate Mode A (pre-issue clearance) into e-Fard for the three districts.
* **Every decision is advisory.** `binding=false`; the response is `ADVISORY`
  for non-Phase-2 districts. A citizen's Fard is never withheld by us, whatever
  the rules say.
* Run Mode B (mirroring) on demand for retrospective review.
* Enrol patwaris and issue field devices.
* Seed real DC rates and real mouza boundaries. **The shipped boundaries are
  bounding boxes; they must be replaced before any real pilot.**

### Why 3 and not 14
Punjab piloted in one district and went province-wide in two months
[9](https://www.dawn.com/news/1995916). Three districts are enough to
encounter tribal-tract, urban-periphery and remote-valley failure modes
(Peshawar, Mardan, Swat respectively) while small enough that the team can
personally review every RED flag the engine raises in the first month.

### Staffing
| Role | Headcount | Note |
|---|---|---|
| Field (patwaris) | 6 seeded, ~40 in-district | Existing staff; no new hires |
| Tehsildar reviewers | 3 (one per district) | Own the RED queue |
| Fraud analyst | 2 | Tune thresholds, review false positives weekly |
| Integration engineer | 2 | One on the e-Fard side, one on ours |
| Helpdesk | 4 | Two Pashto, one Urdu, one English |
| **Programme lead** | **1** | Named, accountable, single point of escalation |

### Exit gate (all must hold for 8 consecutive weeks)
1. ≥ 95% of khasras in the three districts have a digitised boundary and a
   scanned latha, with the residue explicitly enumerated.
2. False-positive rate on RED flags < 15%, measured by Tehsildar review
   outcome. Above this, patwaris learn to ignore RED.
3. ≥ 1,500 accepted ground verifications, ≥ 80% scoring MEDIUM or above.
4. Clearance p99 < 300 ms for 4 consecutive weeks.
5. Zero incidents where our unavailability delayed a Fard.
6. Helpdesk first-response SLA met for 8 weeks.

### Rollback trigger
Any two consecutive weeks with clearance availability < 99.5%, or a false
positive rate above 35%, or a confirmed case where an advisory decision
materially misled a citizen.

Rollback is: set `KLRI_ACTIVE_PHASE=PHASE_0_PILOT` and
`KLRI_PILOT_DISTRICTS=` (empty). Every request then returns an immediate
pass-through CLEAR. **One environment variable, no deploy, no data loss.**

---

## Phase 1 — Dual-run (12 months minimum)

### The core commitment
**The manual register remains fully operational and legally valid for the
entire 12 months.** Nothing is suspended. Nothing is repealed. No rule 36-A
equivalent. We are a verification layer sitting beside a working system, not a
replacement for a working system.

This is the single most important sentence in this document. Punjab suspended
Fard issuance under rule 36-A of the PLRA Rules 1968 [9](https://www.dawn.com/news/1995916)
and had to restore it ten days after province-wide mandate
[5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/).
We will not suspend anything.

### Scope
* Extend to 6–8 districts (add Abbottabad, Mansehra, Nowshera, Charsadda —
  districts with UN-Habitat GIS Lab / Service Delivery Centre coverage
  [8](https://unhabitat.org.pk/digitization-of-land-records/)).
* Both systems run in parallel for every transaction. Divergence is logged,
  not resolved in favour of either one.
* Divergence reporting to the Board of Revenue monthly: where the two systems
  disagree, why, and which was right.
* Public feedback channel live (the e-Fard Digital Public Feedback System
  already routes complaints to Deputy Commissioner dashboards as a monthly
  KPI [3](https://propakistani.pk/2026/10/03/kp-launches-e-fard-portal-for-easy-access-to-land-services/)).
* USSD *444# live in Pashto, Urdu and English.

### What dual-run buys us
A year of labelled data: for every transaction we know what the manual process
produced and what our rules said. That is the only honest way to tune a
threshold, because the fraud we are hunting has no ground truth anywhere else.

### Exit gate
1. Zero unexplained divergences for 3 consecutive months.
2. Dual-run covers 100% of mutations in the enrolled districts.
3. ≥ 90% of patwaris in enrolled districts active in the field app weekly.
4. Every RED flag from the last quarter has a written disposition.
5. Public complaint rate attributable to our layer < 0.5% of transactions.

### Rollback trigger
Any district where divergence exceeds 5%, or where a Tehsildar reports that
our flags are being worked around rather than resolved.

---

## Phase 2 — Enforcement (12–18 months)

### Scope
* Decisions become **binding** per-district, one district at a time, only after
  that district individually passes G-A through G-E.
* `binding=true` → e-Fard honours HOLD and BLOCK.
* Publish **Clearance & Modification** guidance incorporating pilot feedback
  before the first binding district goes live. The feedback loop is the point:
  the rules are wrong in ways only patwaris can see, and they get a formal
  channel to say so before enforcement begins.
* Gradual replacement of the manual route, district by district, with the old
  register retained as a **read-only archive**, never deleted.

### Clearance & Modification (issued after pilot feedback, not before)
1. **Clearance certificate** — a signed record that a mutation passed all rules
   at a given time, with its ledger hash and Merkle inclusion proof.
2. **Modification procedure** — the lawful correction path
   (`db/migrations/002_append_only.sql::record_correction`). Corrections append;
   they never overwrite. The original entry stays visible, exactly as a Fard
   Badar on paper leaves the original entry legible.
3. **Appeal** — any HOLD or BLOCK may be appealed to the Tehsildar within 30
   days. The appeal outcome is itself written to the chain.

### Exit gate
1. 8+ districts binding for 6 months with no reversal.
2. Court has accepted a chain verification report in at least one proceeding
   (admitted for what it is: evidence of what the record said and when).
3. Funding secured for 3 years of operations, not just for build.

### Rollback trigger — **the fail-safe that Punjab did not have**
Punjab's suspension of the GPC in August 2026 required an Assembly-level
decision and was challenged on whether the government could lawfully suspend
an assembly-approved measure [1](https://propakistani.pk/2026/08/28/punjab-suspends-digital-land-record-certificates-for-6-months/).
Rollback must not require a political decision.

Ours takes one configuration change:

```
KLRI_ENFORCEMENT_ENABLED=false
```

Every district immediately reverts to advisory **and** a
`ROLLBACK_ACTIVATED` event is written to the chain, so the record of when
enforcement was on and off is itself tamper-evident. No deploy, no migration,
no data loss, no legislation.

---

## Phase 3 — Legal and funding cover

### 3.1 Legal: draft KP Revenue Act amendment
Full drafting notes in [06-legal-amendment-draft.md](06-legal-amendment-draft.md).
Model: Punjab's draft Registration Act rewrite, which authorises electronic
records, e-seals, digital time-stamps and biometric verification, gives
computer-generated records full evidentiary value, and empowers the Registrar
to cancel fraudulently registered documents
[6](https://www.thenews.com.pk/amp/1354035).

**The order matters: legal cover first, then enforcement.** Punjab legislated
after enforcing, and paid for it.

Four provisions:
1. The integrity ledger is the authoritative record of **what the land record
   contained at a given time** — not of title.
2. A hash-verified record is admissible without further proof of custody;
   an unbroken chain raises a rebuttable presumption of integrity.
3. Ground verification evidence is admissible, with a defined standard for the
   trust score.
4. Tampering with the ledger, or procuring a false ground verification, is a
   distinct offence with defined penalties.

### 3.2 Funding
| Item | Notes |
|---|---|
| Field hardware | Ruggedised Android per patwari; the PWA is the low-cost path but a native wrapper is needed for Play Integrity attestation in Phase 2+ |
| Connectivity | Offline-first design keeps the data bill near zero; budget for device replacement, not for data |
| Anchoring | Publish Merkle roots in the BoR Gazette and on the BoR website. Cost is a notification, not a blockchain |
| Operations | 3-year minimum. A system that detects fraud and has nobody to review the flags is worse than no system: it creates a paper trail of negligence |
| Independent audit | Annual, external, with the audit report itself anchored |

---

## Accessibility — non-negotiable at every phase

| Channel | Who | Phase |
|---|---|---|
| Pashto UI in the field app, Pashto-first default | Patwaris | 0 |
| Urdu and English | All | 0 |
| **USSD *444#** — status check, fraud report, language switch | Citizens without smartphones or data | 1 |
| SMS acknowledgement with a reference number | Same | 1 |
| e-Fard portal, NADRA e-Sahulat, Dastak app | Smartphone users | Already live [3](https://propakistani.pk/2026/10/03/kp-launches-e-fard-portal-for-easy-access-to-land-records/) |
| Helpline staffed in Pashto | Everyone | 1 |

The USSD menu is Pashto by default because the default language of a system
decides who it is for:

```
*444#  →  ۱ د ځمکې حالت   ۲ درغلي خبر کړئ   ۳ ژبه   ۴ مرسته
*444#1*512*MZ-PSH-001  →
   بند شوی. انتقال ودرول شو. سمدستي د ډپټي کمشنر دفتر سره اړیکه ونیسئ.
```

---

## What we are explicitly NOT doing

1. **Not replacing e-Fard, LRMIS or the manual register.** We are a filter and
   a tape recorder.
2. **Not suspending any existing process.** Punjab suspended Fard issuance; it
   was restored within ten days
   [5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/).
   We will not repeat this at any phase.
3. **Not fixing historical title defects.** No comprehensive settlement survey
   has been done in 125 years [11](https://tribune.com.pk/story/2620479). That
   is a settlement programme's job. We flag the defect classes we can detect
   and route them to a human.
4. **Not using a blockchain.** A blockchain buys trust among parties who do not
   trust each other. The Revenue Department is one party. A signed, published
   Merkle root gives the same tamper-evidence at a fraction of the cost, and a
   citizen can hold the inclusion proof on a printed Qasid receipt.
5. **Not claiming to prove GPS authenticity from a web browser.** We cannot.
   See [05-threat-model.md](05-threat-model.md).
6. **Not storing CNICs.** Salted hashes only, for linkage and repeat detection
   (`db/migrations/001_schema.sql`).
