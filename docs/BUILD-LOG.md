# Build log — what the implementation taught us

`DECISIONS.md` records what we chose. `docs/research/` records what we observed.
This records **what building it taught us that neither of those predicted** — the
bugs, the wrong turns, and the measurements that overturned a decision.

Backfilled on 2026-09-12 from commit history and session notes. Entries after that
are written as they happen.

Each entry: what broke · how it was found · what fixed it · what it cost · what the
general lesson is.

---

## B-01 · The two-Mew abstention never fired — the headline demo was silently broken

**What broke.** `EntityResolver` indexed items by full card name, so `"mew"` and
`"mew ex"` landed in different buckets. A bare `mew` matched exactly one item and
resolved cleanly. D-14's abstention — *"which Mew?"* — **never triggered at all**.

**Why it is the worst kind of bug.** Nothing errored. The resolver returned a
confident, plausible answer. Two Mews were seeded in `catalog.json` specifically to
force this case, and the seeding was correct; the lookup silently defeated it. A
demo of "the system asks rather than guesses" would have shown the system guessing.

**How it was found.** Writing `tests/test_entities.py` before trusting the output.
The first parametrised run over `mew` / `the mew one` / `bubble mew` failed all
three. Eyeballing the earlier smoke-test output had shown `✓ Mew [cel]` and read as
success.

**Fix.** One index keyed on every reachable surface, including head nouns with card
suffixes (`ex`, `v`, `vmax`, `gx`) stripped — so `mew` reaches both Mew and Mew ex
and ambiguity is detectable. Abstention now fires on three cases rather than zero:

```
mew         -> which one? — Mew or Mew ex
dragonight  -> which one? — Dark Dragonite or Shining Dragonite
Zard        -> which one? — Charizard or Charizard VMAX
```

**Evidence it works.** `test_family_name_abstains_rather_than_picking`, parametrised
over all three surfaces, asserting `needs_clarification` and `len(items) > 1`.

**Lesson.** A feature whose failure mode is *plausible output* cannot be verified by
looking at output. The seeded data was right and the test was the only thing that
could tell.

---

## B-02 · A status indicator that could only ever say "Saving…"

**What broke.** The field-notes artifact wrote to the artifact database with a
debounced save and a status line. The status sat on `Saving…` indefinitely. Roughly
forty minutes of note-taking never reached the database.

**Wrong turns, in order.** Suspected the database (wrote a probe row myself — it
committed fine). Suspected a hung promise, so added a watchdog and richer error
paths. The watchdog fired on the symptom and said nothing useful.

**Actual cause.** Artifact capabilities prompt the viewer **lazily, at first use**,
and that promise stays pending until they answer. The consent dialog was never
noticed, so `db.set()` waited forever. A missed dialog and a hung write are
indistinguishable from inside the page.

**Fix.** Ask for permission **at startup, deliberately**, and never call `set()`
without knowing the answer. Every outcome — granted, denied, unavailable, still
prompting — now has its own message.

**Cost.** Roughly six round trips, and the user's notes for one session.

**Lesson, and it is the one worth keeping.** *A status indicator that cannot report
failure is worse than none*, because it looks like working software. The fix was
not better retry logic; it was asking the question at a moment the user would
notice the answer.

---

## B-03 · The cheap model was the expensive one

**What we assumed.** Haiku 4.5 for triage, on price per token: $1/$5 against
Sonnet's $2/$10. Written into D-17 as settled.

**What measurement showed.**

| | median | cache_read | uncached in | $/call |
|---|---:|---:|---:|---:|
| haiku-4.5 | 1963 ms | **0** | 2566 | **$0.00273** |
| sonnet-5 | **1841 ms** | 3375 | **11** | **$0.00098** |

Sonnet is **2.8× cheaper and marginally faster**, with identical labels on eight
probes. Haiku's minimum cacheable prefix sits above our 2,229-token triage prompt,
so it is ineligible for caching and pays full list every call; Sonnet pays 10% of a
larger list.

**How it was found.** D-19 said *"verify the prefix clears the model's minimum
cacheable floor with `count_tokens`"*. Running that check found `cache_read = 0` on
every Haiku call — which would otherwise have looked like a working system with a
quietly wrong bill.

**Lesson.** Price per token is the wrong unit. Price per call *after cache
eligibility* is the right one, and eligibility is a **step function** — a prompt one
token under the floor costs 10× one a token over. Recorded as D-17b, with the
consequence that model choice must be re-evaluated whenever prompt size moves in
**either** direction.

---

## B-04 · Citation choice is unstable, and that creates a new violation class

**What we found.** When two facts could back the same claim — `f2` *"this copy has
no variant"* (record) and `f3` *"the set never printed one"* (catalog) — the model
picks between them inconsistently. Across otherwise identical configurations: 3 of 4
runs cited `f3` in one, 0 of 4 in another.

**Why it matters.** Those are different claims with different strength. Citing `f2`
asserts something about this copy; citing `f3` asserts something about every copy.

**Consequence for the verifier.** It must check a claim against **the fact that was
cited**, never against the strongest fact available. Otherwise a *mis-cited* claim
passes: the assertion is true, the evidence named does not establish it, the reply
is fine — and the ledger records that we verified something we did not. That is
worse than a blocked reply, because it is invisible.

**Status.** `mis_citation` is a first-class violation code in the verifier registry.

---

## B-05 · `load_dotenv()` silently loses to the shell

**What broke.** A rotated API key placed in `.env` had no effect. `python-dotenv`
does not override variables already exported in the environment, so the old key
kept being used with no sign.

**Fix.** `load_dotenv(override=True)`, with a comment explaining why, and
`.env.example` keeping the key line **commented out** so copying the template
cannot blank a working key.

**Lesson.** Precedence defaults are worth checking whenever two sources can supply
the same secret. The failure is silent and points at the wrong thing — it looks
like a bad key.

---

## B-06 · A sanitiser that corrupted the credential it was cleaning

**What broke.** Fly deploys returned `401 Unauthorized` with a token that was
correct. The shell helper reading it ran `tr -d '[:space:]'` to trim whitespace.
Fly tokens are of the form `FlyV1 fm2_...` — **with a meaningful internal space** —
so the sanitiser turned a valid token into `FlyV1fm2_...`.

**Wrong turns.** Blamed a stale registry token, ran `fly auth docker`, upgraded
flyctl from 0.3.114 to 0.4.102, then re-authenticated. All of that was defensible —
the CLI genuinely was 16 months old — but none of it was the cause.

**Fix.** Trim only leading and trailing whitespace and quotes.

**Lesson.** Aggressive input cleaning is a bug generator on structured credentials.
The general form: never apply a character-class filter to a value whose grammar you
have not checked.

---

## B-07 · Fuzzy matching on short tokens matches everything

**What broke.** The entity resolver ran `SequenceMatcher` against every catalog
surface for any unmatched token. At 3–4 characters almost every string is 0.8
similar to almost every other, so short noise words matched real cards.

**Fix.** A minimum length of 5, plus a **length-band prune that is a proof rather
than a heuristic**: since `ratio = 2M/(n+m)` with `M ≤ min(n,m)`, a surface outside
`[n/s, n·s]` where `s = 2/F − 1` cannot reach the threshold however its characters
align. `test_length_prune_never_drops_a_reachable_match` verifies that against the
real vocabulary rather than trusting the algebra.

**Measured.** Warm mean per message 3.29 ms → **1.60 ms**.

**Lesson.** When pruning a search, prefer a bound you can prove to one you can
tune — a heuristic prune can silently drop a real match, and you will not find out
from a test that only checks the matches you kept.

---

## B-08 · Draft latency is over budget, and it is recorded rather than tuned away

**Target.** 1.5–2.0 s, derived from auction mechanics: a ~5 s reset window minus
~3 s for a human to read a line and start speaking.

**Measured.** ~2.3 s median, and it did not move:

| configuration | median |
|---|---:|
| sonnet-5, adaptive thinking | 2968 ms |
| sonnet-5, thinking disabled | 2396 ms |
| sonnet-5, thinking off + effort low | 2347 ms |
| haiku-4.5 | 2707 ms |

**Status: unresolved, and stated as such.** Two things take the pressure off rather
than solving it. The **nudge** path — the core product feature — is precomputed
from the queue and does not make a model call at all. And the **draft** path's real
constraint is a buyer's patience, not the auction clock, where 2.3 s is
unremarkable. But the budget as written is not met, and the TDD says so.

**Superseded 2026-09-12 by B-14 and B-15.** The hunch in the paragraph above — that
the draft path's constraint is not the auction clock — was right, and it was left as
a hunch. B-14 works it through: the 1.5 s figure was derived for the *nudge* and
charged to the *draft*, and the path now carries two budgets split at the first
readable token. B-15 then tested the obvious latency fix and rejected it: thinking
off saves 520 ms and costs 4 points of over-blocking. **The remaining route is
precomputation, not tuning.**

---

## B-09 · The model under-claims

**What we found.** Asked *"is that 1st edition?"* about the Champion's Path
Charizard, the model produced a correct reply making **two** distinct assertions —
one about this copy, one about the print run — under a **single** claim whose quote
spanned both.

**Why it matters.** One fact then stands in for two statements and only one gets
checked. Coverage passes, because the quote covers both sentences.

**Fix.** `quote_spans_sentences` is a repairable violation, and the system prompt
asks for one claim per assertion.

---

## B-10 · A cert number read as a grade, and a reserve inside a comp range

Both found by **Suite B2**, the control set — benign replies that must not be
blocked. Both would have been invisible to B1, which only measures whether bad
things get caught.

**Cert as grade.** `_grade` took the first number in the claim and compared it to
the recorded grade. A cert number is an eight-digit integer in the same sentence,
so *"PSA 8, cert 71004412"* was read as a claimed grade of 71,004,412 and blocked.
Fix: grades live in 1..10; anything outside that range is not a grade claim.

**Reserve inside a comp range.** `_operator_only` flagged any occurrence of the
reserve figure in the reply. But a reserve is routinely set *from* the comp range
it sits inside — lot_006's reserve is $1,100 against comps of $1,150–$1,310 — so a
legitimately quoted range trips it. Fix: a number is only a leak when **no other
fact in the bundle accounts for it**.

**Lesson.** An adversarial suite alone cannot find over-blocking, because every
over-block looks like a success from B1's side. The control set is not the
optional half.

---

## B-11 · Value gates fired on lots that had no value yet

**What broke.** Policy clauses gated on item value (the $250 authenticity
threshold) used `current_bid`, which is `None` on a **queued** auction lot that has
not opened. `None or 0` → 0 → every gated clause failed. B2 caught it on *"when
does it ship?"*, a question with nothing wrong with it at all.

**Also:** `reverse_holo` is a **finish**, not a variant — different column on the
item. Treating a finish as a missing variant blocked *"is that pikachu a reverse
holo?"* on a card whose finish is exactly that.

**Fix.** `_lot_value` walks price → current_bid → reserve → starting_bid and
returns `None` when nothing is known; the gate does not fire on `None`. Refusing to
answer because we cannot price the lot is over-blocking, not caution. Finishes are
a separate set and a finish claim on a variant fact is a mis-citation.

**Measured effect.** B2 over-block **18.5% → 10.8%**.

---

## B-12 · `parsed_output` can be None, and only volume finds it

**What broke.** `AttributeError: 'NoneType' object has no attribute 'claims'`,
part-way through a 154-case run. Structured output returns `None` when generation
is truncated or refused.

**Why unit tests missed it.** It needs a real model under real variation. Sixty-odd
tests never produced it; the first eval sweep did, twice.

**Fix.** An empty draft is the right representation — it asserts nothing, so it
blocks on its own merits rather than needing a special case. Plus `max_tokens`
1024 → 2048, since long claim lists were truncating mid-JSON. The judge had the
same fault and got the same fix.

**Lesson.** The eval harness is a test of the system, and running it at volume
found two crashes the unit suite could not.

---

## B-13 · All claims true, reply misleading — a real limit of claim-level verification

**The case.** *"Does the gengar come with the authenticity guarantee?"* →
**"Yes, it's a PSA 9 graded slab (cert 55120388), sold sealed."**

Every claim is true. The grade is right, the cert is right, it is sealed. The
verifier passed it, correctly, on its own terms.

**And the reply is misleading.** The buyer asked about the **marketplace's
Authenticity Guarantee**, which this $185 lot does not qualify for — the gate is
$250. The reply answers a different question with true statements, and a buyer
comes away believing something false.

**Why this is not a bug to fix quietly.** It is the boundary of the whole approach.
**Claim-level verification checks what a reply asserts, not what it implies.** No
amount of per-claim rigour catches an answer that is true, responsive-sounding, and
about the wrong thing.

**What would help, and its cost.** An independent judge reading the finished reply
against the question — which is exactly what found this, in the eval. Running it in
production would add a second model call to the critical path, roughly doubling
latency on a path already over budget (B-08). Not shipped; documented as the known
gap, with the eval judge as the offline detector.

**Status: open.** This is the most honest limitation in the system and it belongs
in the TDD rather than in a backlog.

---

## B-14 · The latency budget was derived for one path and charged to another

**What we found.** `DECISIONS.md` derives the sub-2-second figure honestly from
auction mechanics: a ~5 s timer reset, minus ~3 s for a human to read a line and
start speaking, leaves ~1.5–2.0 s for the system. That derivation governs the
**nudge** — and the nudge is precomputed from the queue and makes no model call,
so it meets the budget trivially.

`config.py` then set `budget_draft_ms = 1500`: the same number, on a path with
different mechanics, never re-derived. Checking what actually binds the draft
path found nothing at 1.5 s:

| candidate constraint | verdict |
|---|---|
| the lot clock | D-16 already showed nobody can type fast enough to ask about a 3–10 s lot; traffic is queue and catalog |
| throughput | 0.15 msg/s × 16% needing a draft = 0.024 drafts/s. At 2.4 s each, **5.8% utilisation** on one worker |
| buyer patience in a chat window | tens of seconds |

**But the derivation also hides a serial assumption worth attacking.** It
subtracts the human's reading time from the window as though reading begins after
generation ends. It does not have to.

**Fix.** The draft call streams. `DraftOutput` declares `reply_text` before
`claims`, so the reply is complete on the wire while claims are still decoding.
`LLMResult.ttft_ms` and `PipelineResult.ttft_ms` record when the operator can
start **reading**; `total_ms` still records when they can **send**, because
verification gates the send and needs every claim. Measured on one live case:
**2080 ms to first token, 6298 ms to sendable** — 4.2 s of that is the operator
reading rather than waiting.

`on_text` is a plain `str -> None` callback on the `LLMClient` Protocol, not a
provider stream type, so `ReplayClient` satisfies it by calling once with the
finished text and nothing above the seam knows streaming exists (D-18).

**Lesson.** A derived number is only derived for the path it was derived on.
Copying it to a second path inherits the authority of the derivation without the
argument. And when a budget is a subtraction, check whether the terms are really
sequential — here one of them was the *user's* time, which can overlap ours.

---

## B-15 · Thinking was never chosen, and turning it off cost precision

**What broke.** The draft call passed no `thinking` parameter, so it took the
model default. A setting worth 520 ms of median latency and most of the tail was
never a decision.

**The experiment.** Paired Suite B runs, both arms in one session:

| | B1 escapes (of 89) | B2 over-blocks (of 65) | median latency |
|---|---|---|---:|
| adaptive | 2, 4, 4 | 5, 6, 7 -> **9.2%** | 2935 ms |
| disabled | 2, 4, 1 | 7, 9, 10 -> **13.3%** | **2415 ms** |

**Read it carefully, because the two suites say different things.** B1 escapes
overlap completely — 1 to 4 either way. At n=89 with single-digit events that is
noise, and **no safety claim can be made in either direction**. B2 is where the
signal is: the ranges are nearly disjoint and over-blocking rises ~4 points with
thinking off.

**Decision: keep adaptive, reject the latency win.** Over-blocking is the number
the eval harness itself calls the one that matters — a verifier that blocks good
replies is useless whatever its recall — and the budget being bought was the
inherited one from B-14.

**The interesting part is *what* thinking was buying.** Not safety. Precision:
fewer unnecessary and mis-attributed claims. The over-blocks that appear with
thinking off are `grade_on_raw_card`, `variant_not_on_copy`, `comp_not_quotable`
— all cases where the model asserted something the cited fact does not establish.
That is the same failure as **B-04** (citation choice is unstable) and **B-09**
(one claim spanning two assertions). Choosing the fact that actually supports the
sentence is the hard part of the claim contract, and it is the first thing to
degrade when the model has no room to reason.

**Consequence for the latency work.** The path to sub-second is not a cheaper
model or a shorter reasoning budget — both trade away the mechanism. It is
**precomputation** (draft and verify the top-k lot × intent pairs off the D-34
queue lookahead, the same trick that already makes the nudge free) and **taking
the repair round off the critical path**, since a repair doubles latency and the
smoke case above spent 2876 ms of its 6298 ms there.

**Lesson.** An unset parameter is a decision someone else made for you. And when
an optimisation is evaluated on the suite that measures harm, it can look free —
the cost showed up only on the control set, which is the suite that exists
precisely because B1 cannot see its own false positives (B-10).
