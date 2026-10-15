# Punjab's land record digitisation: what went wrong, and what we do differently

*This document is a design input, not commentary. Every design decision in the
rollout plan traces back to a numbered failure here.*

---

## 1. The timeline

| Date | Event |
|---|---|
| 2006 | World Bank–funded Land Record Management Information System (LRMIS) begins data entry |
| 2017 | LRMIS project closes: 91.5% of 25,258 mauzas digitised, **8.5% unresolved** [2](https://www.dawn.com/news/1533515) |
| 2017 | Punjab Land Revenue Authority (PLRA) Act succeeds LRMIS |
| ~2018–19 | Arazi Record Centres take over mutation entry from patwaris; Rs17bn revenue in FY2018-19; only ~600 complaints on the PM portal [2](https://www.dawn.com/news/1533515) |
| Feb 2020 | Board of Revenue begins **reintroducing the patwari system** in pilot circles (Mandra, Chakri). Officials warn of "social chaos" and small farming families being pushed out [2](https://www.dawn.com/news/1533515) |
| Oct 2025 | Punjab Cabinet committee backs a draft rewrite of the Registration Act 1908: electronic records given full evidentiary value, e-seals, digital time-stamps, biometric verification defined [6](https://www.thenews.com.pk/amp/1354035) |
| Apr 2026 | Green Property Certificate (GPC) pilot announced; Fard issuance to be suspended under rule 36-A of PLRA Rules 1968, starting **Sahiwal on 1 May**, then Lodhran and Hafizabad on 1 July, whole province by December 2026 [9](https://www.dawn.com/news/1995916) |
| 1 Jul 2026 | GPC made **mandatory province-wide**. Procedure: surveyor verifies ownership, possession and location; 15-day online public notice for objections; Rs950 fee. Public complaints about the complexity of Fard Taqseem/vanda Taqseem and rent-seeking surveyors [10](https://tribune.com.pk/story/2615842) |
| 11 Jul 2026 | **Partial reversal.** Fard issuance restored in non-digitised areas; GPC no longer mandatory there. PLRA Deputy Director Ali Raza Butt states ~96% digitised, the remaining 4% stuck on disputes and record deficiencies. Affected: Lahore outskirts, Sheikhupura, Kasur, Nankana Sahib, Okara, Sahiwal, Bahawalpur, Muzaffargarh, Layyah, Rajanpur, DG Khan, Mianwali, Bhakkar [5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/) |
| Jul 2026 | Punjab patwari association (Shahid Ansar Goraya): no comprehensive settlement survey in 125 years; 2007-era data-entry errors worsening problems; indivisible records, multiple properties under one survey number, outdated boundary demarcation [11](https://tribune.com.pk/story/2620479) |
| 28 Aug 2026 | **Punjab suspends the GPC system for six months.** Opposition leader Rana Aftab Ahmed alleges corruption and praises the patwari system; MPA Raja Shaukat Bhatti reports multiple people shown as owners of the same two-acre plot; the Assembly Speaker questions whether the government can suspend an assembly-approved measure [1](https://propakistani.pk/2026/08/28/punjab-suspends-digital-land-record-certificates-for-6-months/) |

---

## 2. The five failures

### F1 — The new system was made mandatory before it could serve every case

The GPC was announced in April, piloted in one district in May, and made
mandatory **province-wide on 1 July**. Ten days later, Fard issuance had to be
restored in non-digitised areas [5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/).

The failure is not that the GPC was a bad idea. It is that the *cutover* was
scheduled against a calendar rather than against a coverage threshold. The
unresolved 4% was not uniformly distributed — it concentrated in exactly the
places where disputes, inheritance and deficient records cluster, i.e. the
cases most needing a working process [5](https://propakistani.pk/2026/07/11/punjab-restores-manual-land-record-certificates/).

> **Design consequence:** our rollout is gated on exit criteria measured in
> data, not on dates. See §3 gates.

### F2 — Removing the fallback removed the safety net

Rule 36-A of the PLRA Rules 1968 was used to suspend Fard issuance so the GPC
would be the sole valid instrument [9](https://www.dawn.com/news/1995916). Once
the alternative was gone, any case the GPC could not handle had nowhere to go.

The Punjab experience since 2017 shows how much institutional churn this
created: powers of mutation entry moved from patwaris to Arazi Record Centres
and then back to patwaris in 2020, with officials warning of "social
chaos" [2](https://www.dawn.com/news/1533515).

> **Design consequence:** the manual register stays legally valid for the whole
> of Phase 0–1. We are a verification layer, never the only route. This is
> enforced in code: `binding=False` and `decision=ADVISORY` for every district
> not yet in Phase 2.

### F3 — Ground truthing was outsourced to a paid, unaudited step

The GPC procedure required a surveyor to verify ownership, possession and
location, then a 15-day public notice [10](https://tribune.com.pk/story/2615842).
Rs950 fee. Complaints followed about rent-seeking surveyors and about the
complexity of Fard Taqseem/vanda Taqseem.

A paid human verification step with no independent evidence trail is a worse
than useless anti-fraud control: it launders the transaction through an
official-looking stamp.

> **Design consequence:** ground verification produces *evidence* — GPS fix,
> accuracy, camera-original photograph with SHA-256, two witness CNIC hashes
> checked against NADRA, a device-attested payload — and that evidence is
> scored against twelve signals. Nobody stamps anything "verified"; the score
> is computed and stored.

### F4 — Old unresolved record defects were never fixed, only papered over

The patwari association's account is the most damning: no comprehensive
settlement survey in 125 years, 2007-era data-entry errors still propagating,
indivisible records, multiple properties under a single survey number, and
outdated boundary demarcation [11](https://tribune.com.pk/story/2620479).

Similarly, the LRMIS project closed with 8.5% of mauzas unresolved [2](https://www.dawn.com/news/1533515) —
and that residue is still producing cases a decade later.

> **Design consequence:** we do not claim to fix the record. We flag the
> *specific* defect classes we can detect (oversubscribed shares, rapid
> re-mutation, ownership that contradicts the scanned latha) and route them to
> a human with a written-reason requirement. Fixing title is a settlement
> programme's job.

### F5 — Corruption allegations attached to the new system, not the old one

MPA Raja Shaukat Bhatti reported multiple people shown as owners of the same
two-acre plot; the opposition leader alleged corruption in the GPC and praised
the patwari system it replaced [1](https://propakistani.pk/2026/08/28/punjab-suspends-digital-land-record-certificates-for-6-months/).

Note what happened politically: the new system absorbed the blame for errors it
did not create, because it could not distinguish "this record was always
wrong" from "this record was tampered with". That distinction is exactly what a
hash chain provides, and Punjab did not have it.

> **Design consequence:** this is the single strongest argument for the ledger.
> When the same two-acre plot shows two owners, the question a minister needs
> answered in an hour is *"was this tampered with, or was it entered badly in
> 2007?"* `scripts/demo.py` scene 5 answers it in one command.

---

## 3. The gates we adopt instead of a calendar

No phase advances until its gate is met. A gate is a number, measured weekly,
published to the Board of Revenue.

| Gate | Requirement |
|---|---|
| **G-A: coverage** | ≥ 95% of khasras in the district have a digitised boundary AND a scanned latha, with the remainder explicitly listed |
| **G-B: quality** | Zone A+B (clean and ambiguous) ≥ 90% of mutations; oversubscribed-share rate < 1% |
| **G-C: operations** | Clearance p99 < 300 ms for 4 consecutive weeks; zero unexplained fail-closed incidents |
| **G-D: field** | ≥ 80% of site visits score MEDIUM or above; ≥ 1,500 accepted visits in the district |
| **G-E: people** | ≥ 90% of field staff trained, measured by a completion record, not attendance; helpdesk SLA met for 8 weeks |
| **G-F: legal** | For Phase 3 only: the amendment is in force, not merely drafted |

Punjab's schedule (Sahiwal 1 May → Lodhran/Hafizabad 1 July → all Punjab
1 July [9](https://www.dawn.com/news/1995916)) met none of these, because it
was a date list.

---

## 4. The one thing Punjab got right

Punjab's draft Registration Act rewrite is the model for our Phase 3: it
authorises PLRA to run an electronic record system, defines e-seals, digital
time-stamps and biometric verification, gives computer-generated records full
evidentiary value, and empowers the Registrar to cancel fraudulently registered
documents [6](https://www.thenews.com.pk/amp/1354035).

That is the correct order of operations — **legal cover first, then
enforcement**. Enforcing first and legislating afterwards is what allowed the
Speaker to ask whether the government could lawfully suspend an
assembly-approved measure [1](https://propakistani.pk/2026/08/28/punjab-suspends-digital-land-record-certificates-for-6-months/).

Our Phase 3 follows Punjab's good idea, not Punjab's schedule.
