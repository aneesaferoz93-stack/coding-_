# The Fraud Rules Engine

> **Design stance: no machine learning in the decision path.**
>
> Every finding this engine produces can be explained in one sentence with the
> numbers that triggered it. That is a deliberate trade. A gradient-boosted
> model might catch fraud patterns we have not thought of — and it would be
> useless in a revenue court, unappealable by a citizen who wants to know why
> their sale was stopped, and untunable by a Tehsildar who disagrees.
>
> ML belongs *alongside* this engine as a prioritisation aid for the review
> queue, never inside the decision. Every finding carries
> `config_fingerprint`, so any decision can be reproduced years later.

---

## 1. Rule catalogue

| ID | Name | Default severity | Data required | Legal basis |
|---|---|---|---|---|
| **R1** | Rapid re-mutation | RED | Mutation history | KP Land Revenue Act s.34/35 |
| **R2** | Undervaluation vs DC rate | RED / AMBER | Notified DC rate table | Benami Transactions (Prohibition) Act 2017 |
| **R3** | Seller not viable | RED / AMBER | NADRA life status + age | KP Land Revenue Act — Intiqal-e-Wirasat |
| **R4** | Unverified power of attorney | RED / AMBER | POA registration + NADRA biometric | Power of Attorney Act 1882; Registration Act 1908 s.17/18 |
| **R5** | Identity drift vs latha | AMBER | Scanned pre-computerisation register | — |
| **R6** | Ground verification missing / inadequate | RED / AMBER | `ground_verification` | KP Land Revenue Act s.34 |
| **R7** | Double sale (oversubscribed shares) | RED | `khasra_share` | — |
| **R8** | Patwari velocity | AMBER | Mutation counts per officer-day | — |
| **R9** | Missing identity verification | AMBER | NADRA biometric flags | — |

The first five are the rules the user specified. R6–R9 exist because the first
five alone are trivially bypassable: a fraudster who pays the DC rate, uses a
living seller, transacts in person, and inherits the name from the old latha
passes R1–R5 cleanly. The extra four ask the questions that catch that person.

---

## 2. The rules in detail

### R1 — Rapid re-mutation
**Trigger:** the same khasra is mutated more than once within 30 days
(`rapid_remutation_window_days=30`, `rapid_remutation_max=1`).

**Why it works:** the classic KP double sale is a race. Buyer A registers on
Monday; the seller sells to Buyer B on Friday. Both mutations are individually
well-formed. Only the *velocity* is anomalous.

**Softer band:** 2–3 prior mutations in 60 days → AMBER
(`rapid_remutation_watch_days=60`, `rapid_remutation_watch_max=2`). A
partition or an inheritance distribution legitimately touches a khasra several
times, so the watch band exists to separate partitioning from racing.

**Known false positive:** genuine family partitions, where one khewat is split
into six khasras over a fortnight. Mitigation: R1 does not fire on
`PARTITION` transaction types when the area is conserved across the set.

---

### R2 — Undervaluation vs DC rate
**Trigger:** declared price below 50% of the notified DC valuation → RED; below
70% → AMBER. Transactions below Rs 100,000 are ignored.

**Why:** under-declaration is the mechanism of benami transfer. Two people agree
to a real price of Rs 2 crore and declare Rs 60 lakh; the difference moves in
cash, untraceable. The declared figure is also what the state loses stamp duty
on.

**Honest limitation:** the DC table rate is an administrative valuation, not a
market price. In districts where the notified rate has drifted far above market
(e.g. Swat, where `MZ-SWT-002` is seeded at Rs 650,000/kanal against
`MZ-SWT-001` at Rs 900,000), *every* honest transaction will trip AMBER. This
rule must be tuned per district in Phase 0 using the dual-run data — which is
precisely why Phase 1 lasts twelve months.

---

### R3 — Seller not viable
Fires on, in order of severity:

| Condition | Severity |
|---|---|
| NADRA records the seller as deceased | RED |
| Seller CNIC blocked or cancelled | RED |
| Seller identity never checked against NADRA | RED |
| Seller CNIC not found in NADRA | RED |
| Seller age > 90 (`seller_age_red`) | RED |
| Seller age > 80 (`seller_age_amber`) | AMBER |
| Malformed CNIC | AMBER |

**On the age thresholds:** the brief specified >90 as fraud and >80 as a watch
item, and the code follows that. But be clear about what an age flag is. It is
not evidence of fraud. It is evidence that the *person presenting as the
seller* may not be the registered owner — a nephew attending with an elderly
uncle's CNIC is the common shape. That is why the recommended action is
"re-verify biometrically", not "block".

**A dead seller is different.** If NADRA says the seller died, a sale is
impossible; the only legitimate path is an inheritance mutation
(Intiqal-e-Wirasat) with the full list of legal heirs. This is RED and the
recommended action is BLOCK.

---

### R4 — Unverified power of attorney
**Trigger:** any party acting through a POA where the attorney has not been
biometrically verified at NADRA → RED. Unregistered POA → RED.

**Why:** POA fraud is the highest-value, lowest-effort attack in Pakistani land
records. A forged POA lets someone who has never owned the land convey it to a
buyer who has every reason to believe the paperwork. The attorney is abroad, or
unavailable, or fictitious.

**The fix is cheap:** the attorney must verify biometrically at a NADRA
e-Sahulat centre (already the e-Fard channel network) and the POA must be
confirmed against the registered deed. No new infrastructure.

---

### R5 — Identity drift vs the old latha
**Trigger:** fuzzy name match against the scanned pre-computerisation register
scores below 0.60 (`latha_match_min_score`), or the recorded qaum/caste and
mouza both differ.

**Why the latha matters:** it was written before anyone could know this system
would exist. It is the closest thing the Revenue Department has to an
independent witness, and it cannot be edited by someone with database access
today — it is a scanned image.

**Why caste/mouza is only AMBER, and why this needs care.** In a KP jamabandi,
column 3 (Patti/Taraf/numberdar) encodes physical location, and in many
villages the patti is named after the qaum that lives there. Column 4 carries
owner + father + grandfather precisely because identical names recur within one
village. So a qaum mismatch is a *signal*, not a proof — it may mean the record
was entered wrongly in 2007, or that a family moved, or that patti boundaries
were redrawn at a settlement that predates the scan.

**Therefore R5 never blocks.** It asks a human to reconcile two records, which
is exactly the work a Tehsildar is for. Escalating this to RED would make the
system an engine of harassment in exactly the districts where names are least
distinctive.

**Deliberate allowance:** Khasra 77 in Takht-i-Bhai is seeded in the name of
the deceased father — which is the *normal* state of a KP inheritance record.
R5 has to treat that as "reconcile", not "fraud".

---

### R6 — Ground verification missing or inadequate
**Trigger:** no accepted site visit for SALE, GIFT, PARTITION or EXCHANGE
(`ground_required_for`) → RED. Visit accepted but with weak trust → AMBER.

**Why this is the keystone rule.** Every other rule reasons about paper. R6 is
the only one that asks whether anyone actually stood on the land. A 20-kanal
sale with no site visit is not a suspicion; it is a process failure.

**Why it clears so fast:** in the demo, `MUT-KP-1001` starts at HOLD with 45
points purely because nobody had verified it. After one legitimate site visit
it drops to **CLEAR, score 0**. The flag is resolved by evidence, not by an
override. That is the entire value proposition of the field app: it gives an
honest Patwari a way to clear their own name in minutes.

---

### R7 — Double sale (oversubscribed shares)
**Trigger:** the seller is conveying more than they hold
(`sold_share > held_share`), or the sum of current shares in a khasra exceeds
one.

**Why the database backs this up:** `db/migrations/001_schema.sql` installs a
trigger that raises a WARNING whenever the sum of live shares in a khasra goes
above 1.0. The application refusing is a policy; the database refusing is a
guarantee.

**The real window:** mutations sit in the jamabandi's remarks column and only
enter the main body at the next ~4–5 yearly revision. That gap is where a
double sale lives undetected for years. R7 closes it by checking shares
continuously rather than at revision time.

---

### R8 — Patwari velocity
**Trigger:** more than 12 mutations entered by one patwari in one day
(`patwari_daily_mutation_amber`) → AMBER.

**Reading it correctly:** high velocity is usually a hard-working patwari
clearing a backlog, or a settlement camp. It is a triage signal — "look at this
officer's queue" — not an accusation. Severity stays AMBER permanently for this
reason.

---

### R9 — Missing identity verification
**Trigger:** any party not biometrically verified → AMBER.

**Context:** e-Fard already requires NADRA biometric verification, so in the
normal integrated path R9 should never fire. When it does, it means the
transaction entered through a channel that bypassed e-Fard — which is itself
worth knowing.

---

## 3. Scoring

```
score = clamp(0, 100,  45 × red  +  18 × amber  +  3 × info)
```

**The score is not a probability.** It is a transparent weighted count. Any UI
that renders it as a percentage risk, or as a fraud likelihood, is a bug.

Default weights: RED 45, AMBER 18, INFO 3.

Two REDs (90) is already severe. One RED plus one AMBER (63) is a real signal
that needs a human. Three AMBERs (54) is a "look at this when you have time".
The weights were chosen so that a single AMBER can never hold a transaction,
because a system that stops one transaction in ten on a technicality gets
switched off — correctly.

## 4. Severity → decision, by rollout phase

| Phase | RED → | AMBER → | Binding? | Ground mandatory? | Escalation |
|---|---|---|---|---|---|
| 0 Pilot | ADVISORY | ADVISORY | No | No | Tehsildar (pilot cell) |
| 1 Dual-run | HOLD | HOLD | No (override recorded on the ledger) | Yes | Tehsildar / DC |
| 2 Enforcement | BLOCK | HOLD | Yes | Yes | DC / Board of Revenue |
| 3 Legal cover | BLOCK | HOLD | Yes | Yes | BoR / Revenue Courts |

This table lives in `backend/app/core/decision.py`. It is the file where the
Punjab lesson is encoded in code: **Phase 0 and 1 cannot bind**. See
[03-punjab-postmortem.md](03-punjab-postmortem.md).

## 5. Configuration

All thresholds in `RuleConfig`. Every evaluation records
`config_fingerprint()` — the SHA-256 of the active configuration — so that a
decision made in 2027 can be reproduced exactly with the 2027 thresholds, and
so a silent threshold change cannot retroactively change the meaning of an old
flag.

```python
from app.core.fraud_rules import DEFAULT_CONFIG

swat = DEFAULT_CONFIG.replace(
    undervaluation_amber_pct=Decimal("55"),   # notified rates are high in Swat
    seller_age_amber=75,
    patwari_daily_mutation_amber=18,          # settlement camp in progress
)
```

Per-district configs are the expected Phase 1 outcome. The fingerprint is what
stops that from becoming an untunable mess.

## 6. Adding a rule

1. Write `def rule_ryour_rule(ctx: MutationContext, cfg: RuleConfig) -> list[Finding]`
   in `fraud_rules.py`.
2. Register it in `RULE_REGISTRY`.
3. Add its thresholds to `RuleConfig` **and** to `fingerprint()` — a threshold
   that is not in the fingerprint is a threshold that can change invisibly.
4. Include `evidence` (a dict of the actual numbers) and `recommended_action`.
5. Add a test with at least one firing case and one clean case.

Rules must be individually disableable (`evaluate(..., rules=["R1","R2"])`),
because a rule that turns out to be wrong in the field needs to be switched off
in a district without a deploy.
