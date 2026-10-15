# Threat Model and Honest Limitations

*The purpose of this document is to state precisely what this system cannot do.
An anti-fraud system whose limits are unstated will be trusted past those
limits, and the first person to discover the gap will be the person defrauding
the citizen.*

---

## 1. Assets

| Asset | Why it matters |
|---|---|
| `audit_log` | The integrity guarantee. If it can be rewritten, nothing else holds. |
| `chain_anchor.merkle_root` | Published, signed checkpoints. The external witness. |
| `ground_verification` | Evidence that a human stood on the land. |
| Attestation secrets (`field_device`) | Bind a payload to a named officer. |
| CNIC hashes | Identify parties without storing their numbers. |

## 2. Adversaries

| # | Adversary | Capability | Realistic in KP? |
|---|---|---|---|
| A1 | Outside forger | Forged documents, fake CNICs, impersonation | Yes, common |
| A2 | Complicit patwari | Their own enrolled device, real GPS, real camera — but a false report | Yes, the hard case |
| A3 | Complicit Tehsildar / revenue officer | Can dismiss flags, approve overrides, approve corrections | Yes |
| A4 | Insider with database access | Can UPDATE/DELETE anything not protected | Yes |
| A5 | Insider with DDL rights | Can drop the append-only triggers | Yes — **the one that matters** |
| A6 | Compromised infrastructure | Controls our servers entirely | Low, but plan for it |
| A7 | Coercion | An officer forced at gunpoint to file a false report | Yes, in parts of KP |

---

## 3. Defences and their limits

### 3.1 The hash chain

**Defends against A1, A4, A5.** Every entry carries
`entry_hash = SHA-256(canonical(prev_hash ‖ seq ‖ event ‖ entity ‖ actor ‖ ts ‖ payload))`.
Change any field and every subsequent hash is wrong.

**Verified end to end** in `scripts/demo.py` scene 5, which drops the
append-only trigger, edits a payload, and re-checks:

```
chain after:  intact=False  ██ TAMPERED ██
    [HASH_MISMATCH] seq 5: Stored hash does not match the record contents.
    The payload, actor, entity or timestamp was altered after the entry was written.
```

**Four layers, because one is not enough:**

1. `BEFORE UPDATE OR DELETE` trigger refuses modification.
2. PostgreSQL **rules** (`DO INSTEAD NOTHING`) — survive `ALTER TABLE ... DISABLE TRIGGER`,
   which is the standard "temporary fix" that becomes permanent.
3. `REVOKE UPDATE, DELETE ... FROM klri_app` — the running service physically
   cannot rewrite history even if the code has a bug.
4. **Merkle anchors published externally** (BoR Gazette, BoR website). This is
   the layer that actually defeats A5. An attacker with full database control
   can rewrite the chain consistently — but they cannot rewrite the root hash
   printed in a gazette notification and cached in a hundred places.

**LIMIT — read this carefully:**

> A hash chain proves **internal consistency**, not **truth**. If a false
> mutation is entered on day one, the chain will faithfully prove that the
> false mutation has not been altered since. Integrity is not accuracy.
>
> This is exactly why R6 exists and why ground evidence is scored. The chain
> answers "has this changed?" Ground verification is what answers "was this
> ever right?"

### 3.2 The lawful correction path

A Patwari will need to correct a genuine clerical error. If the answer is "the
database forbids it", the protection gets removed.

`record_correction()` (in `002_append_only.sql`) is a `SECURITY DEFINER`
function that **appends a superseding event** and requires a written reason of
at least 20 characters plus an approver. The original entry is never modified.
This mirrors how a Fard Badar works on paper, where the original entry stays
legible beneath the correction.

**LIMIT:** A3 (a complicit Tehsildar) can approve a corrupt correction. That is
what the published Merkle roots and the annual external audit are for. The
design goal is not to make fraud impossible; it is to make it **leave evidence
that survives the fraudster's tenure**.

### 3.3 Ground verification: the twelve signals

Full list and scoring in `backend/app/core/ground_trust.py`. Signal families:

| Family | Signals | Catches |
|---|---|---|
| GPS | G1–G4 | Mock provider, impossible accuracy, teleport between fixes |
| Geofence | G5 | Officer not at the parcel |
| Photo | G6–G8 | Gallery upload, missing EXIF, reused photograph |
| Witnesses | G9–G10 | Missing / unverified neighbour CNICs |
| Device | G11 | Unregistered handset, failed app attestation |
| Time | G12 | Excessive offline window |

**Why scoring, not gating.** The demo shows the difference:

```
  ██ REJECTED ██  score   0   Mock GPS provider
  ██ REJECTED ██  score   0   Photo uploaded from the gallery
  ██ REJECTED ██  score   0   Teleport: 150 km from the last fix in 2 minutes
  ██ REJECTED ██  score   0   Fix outside the mouza boundary
  ██ REJECTED ██  score   0   Reused photograph (same SHA-256 as an earlier visit)
  ██ REJECTED ██  score  40   Unregistered handset, no attestation
```

No single signal is treated as proof. In rural KP a weak GPS fix or a two-day
offline window is usually **terrain**, not fraud — and a rule that punishes
terrain gets switched off by the people who have to live with it. So each
signal costs points and the visit is rejected on the combined score.

**LIMIT — this is the most important limitation in the system:**

> **A web browser cannot prove a GPS fix is real.** Android's
> `Location.isFromMockProvider()` is not exposed to web code. `capture="environment"`
> is a hint that any mobile browser may ignore. EXIF can be forged. dHash
> detects duplicates, not fabrication.
>
> Everything the PWA collects is a *signal*, and the server scores it as one.
> The PWA's own source header says so plainly.
>
> **Recommended Phase 2+: a thin native Android wrapper using the Play
> Integrity API**, which does provide a hardware-backed verdict on device
> integrity and mock-location usage. Until then, treat a single site visit as
> suggestive, and treat *patterns* (the same device at improbable places, the
> same photo hash across khasras, the same two witnesses across a district) as
> the real signal.

**What the PWA does enforce, honestly:**

| Control | Strength |
|---|---|
| `capture="environment"` | Hint only |
| `image/*` MIME check | Weak |
| `file.lastModified` within 5 minutes | Moderate — a rooted device can set it |
| Photo SHA-256 + 9×8 dHash reuse detection across the whole corpus | **Strong** — this is the control that actually catches recycled photos |
| Device-enrolment HMAC over `nonce:lat:lon:timestamp:photo_sha256` | Moderate — proves it came from *our* app, not that the app was told the truth |
| Per-visit SHA-256 chain in IndexedDB | Moderate — detects tampering with the offline queue on the handset |
| Server-side geofence (PostGIS `ST_Contains`, shapely fallback) | Strong against *sloppiness*, not against a determined fake |

### 3.4 What we do with CNICs

**Never stored.** Only `HMAC-SHA256(cnic, key=ledger_signing_key)`, for linkage
and repeat detection, and only masked forms (`17301-*******-7`) in any output.

A land-records database is precisely the wrong place to keep a province-wide
list of national identity numbers. Theft of that list would be a far greater
harm to citizens than the fraud we are preventing.

**LIMIT:** hashing does not anonymise against a small candidate set — with a
known key and a guessed CNIC you can confirm membership. Key custody is
therefore a real security control, not a formality.

### 3.5 Coercion (A7)

The threat model that is usually omitted and should not be. An officer
attending a disputed parcel in parts of KP can be pressured.

Mitigations: the witness requirement (two neighbours' CNICs verified against
NADRA) means a coerced visit must also coerce or fabricate two independent
parties; every visit is attributable to a device and an officer; and the
appeal path means a citizen can contest a verification they never witnessed.

**LIMIT:** none of this protects the officer at the moment of coercion. That is
a policing problem, and no software fixes it.

---

## 4. What we explicitly do not claim

1. **We do not prove title.** Nothing here answers "who owns this land?" from
   our own data. That remains LRMIS / e-Fard, and legally the manual register
   until Phase 3.
2. **We do not detect all fraud.** We detect the classes in
   [04-fraud-rules.md](04-fraud-rules.md). A patient fraudster who pays the DC
   rate, uses a living verified seller, transacts in person, stood on the land
   and briefed two neighbours will pass. Detecting that person needs
   investigation, not rules.
3. **We do not fix historical defects.** No comprehensive settlement survey has
   been done in 125 years [11](https://tribune.com.pk/story/2620479). We flag
   defects; a settlement programme fixes them.
4. **We cannot stop a determined A5 (DBA with DDL rights) from rewriting the
   database.** We can make it detectable within one anchoring interval and
   provable in court afterwards. That is the correct and achievable goal.
5. **The score is not a probability.** See [04-fraud-rules.md](04-fraud-rules.md) §3.
6. **We cannot prove GPS authenticity from a browser.** See §3.3.

---

## 5. Operational controls that matter more than the cryptography

| Control | Why |
|---|---|
| Separate the anchoring key from the database server | Otherwise a DB compromise also compromises the signature |
| Publish Merkle roots **externally and on a schedule** | An unpublished anchor defends nothing |
| Annual external audit, with the audit report itself anchored | Someone outside the department must check the chain |
| Rotate attestation secrets on device loss | One lost handset should not be a permanent credential |
| Alert on flag-dismissal rate per officer | The quietest corruption signal is an officer who dismisses everything |
| Alert on any `ROLLBACK_ACTIVATED` or `DEGRADED_CLEARANCE` event | Covers the failure mode where the system is quietly bypassed |
| Never cache `/v1/` responses in the service worker | A stale "this khasra is clear" can cause real harm |

---

## 6. Residual risk register

| Risk | Likelihood | Impact | Mitigation | Residual |
|---|---|---|---|---|
| Chain rewritten by A5 | Low | High | Published Merkle roots + external audit | Medium |
| Complicit patwari files a false visit | Medium | High | 12-signal scoring, photo reuse corpus, witness NADRA checks, pattern analytics | Medium |
| Complicit officer dismisses flags | Medium | High | Written-reason requirement enforced in the DB, dismissal-rate alerting, appeal path | Medium |
| Mock GPS in the PWA | High | Medium | Play Integrity native wrapper in Phase 2+; until then, pattern analytics | High until Phase 2 |
| DC rates badly out of date | High | Medium | Per-district tuning in Phase 1, AMBER not RED | Medium |
| System unavailable | Medium | Medium | Fail-open by design, `DEGRADED` recorded and re-checked | Low |
| Rule misconfiguration causes mass false positives | Medium | High | Per-district config, fingerprint on every evaluation, one-env-var rollback | Low |
| CNIC hash key leaked | Low | High | Key held outside the DB, rotatable, hashes are not reversible without it | Low |
