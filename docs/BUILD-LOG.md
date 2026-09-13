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

---

## B-16 · The repair round looked like latency overhead; it is halving over-blocking

**The proposal.** Take the repair round off the critical path. A repair is a
second full model call, and the B-14 smoke case spent 2876 ms of its 6298 ms
there — so make it operator-triggered and stop paying for it inline.

**What the measurement said.** `evals/run_guardrails.py` now reports the funnel,
because nothing had ever measured whether rewriting actually works:

| | fires on | converts to sendable | cost when it fires |
|---|---:|---:|---:|
| B1 adversarial | 12/89 · 13.5% | **9/12 · 75%** | +2941 ms |
| B2 control | 4/65 · 6.2% | **4/4 · 100%** | +7847 ms |

**The proposal was backwards.** Those four B2 conversions are four benign replies
that were blocked and then rescued. Without the repair round, B2 over-blocking
would have been **8/65 (12.3%) instead of 4/65 (6.2%)** — the retry is *halving*
the number the eval calls the one that matters. And no repair in either suite
converted a blocked draft into an escape, which is the failure it could plausibly
have introduced.

**And it costs nothing at the median.** It fires on 6.2% of benign traffic, so
p50 is untouched. What it does is own the tail: +7.8 s when it fires.

**Decision: keep it inline, and state the tail honestly.** Making it
operator-triggered would trade a recovery that works automatically 100% of the
time on benign traffic for a click. Streaming (B-14) already softens the wait —
the operator reads draft 0 as it arrives and sees draft 1 replace it.

**Lesson, and it is the second time this session.** B-15 rejected a latency win
that cost precision; this rejects a latency win that cost *more* precision. Both
were proposed from a stopwatch and refused by the control set. **An optimisation
argued from latency has to be priced against B2, because B2 is the only suite
that can see what it broke.** Also: a mechanism nobody has measured is a
mechanism nobody can cut — the fire rate and the conversion rate were both
unknown until the question was asked, and the intuition about both was wrong.

---

## B-17 · The same None bug, at the second call site

**What broke.** `parsed_output` is None when generation is truncated or refused.
That was found once (B-12) and fixed **at the draft call site**. The first real
Suite A run crashed with `AttributeError: 'NoneType' object has no attribute
'intent'` — the classify path, which had the identical hole.

**Fix, and where it belongs.** In `_envelope`, which every live call returns
through, with the caller supplying its own safe value. Classify's is an
abstention: `confidence=0.0` routes to `unknown`, which surfaces the message
without a draft (D-13, D-14). Dropping it would be the one unsafe option.

**Lesson.** A guard at one consumer is not a fix when the seam has two. The bug
was recorded, understood and still recurred, because the entry said what went
wrong rather than where the fix had to live.

---

## B-18 · The strongest feature is blind to the question it most needs to answer

**What we found.** `catalog_entity` — "does this message name something we
sell?" — is the feature the whole D-15 amendment rests on. Testing it on
`"You got any psyducks"`, a real message from the corpus, returned **nothing**.
Psyduck is not one of our fifteen lots.

**Why that is structural, not a data gap.** `availability_q` is overwhelmingly
*"do you have X"*, and the premise of asking is that X might **not** be in the
catalog. So the best feature is definitionally blind on the class it is most
needed for.

**How it was found.** A unit test, not the eval. The message sat 0.003 under the
threshold and Suite A's aggregate looked healthy — B-01's shape again, where the
symptom of the failure is a plausible number.

**The fix that did not work.** A 221-name species vocabulary, so naming *any*
Pokémon counts whether we stock it or not. It fit well — weight +1.22, above
`catalog_entity`'s +0.47 — and **5-fold cross-validation rejected it**: 75.8%
+/- 2.2% against 77.0% +/- 1.3% without, with a much less stable threshold
(0.67-0.74 vs 0.52-0.61). Reverted.

Why it failed is the interesting part: plenty of noise names a Pokémon.
*"dragonite has been staring at me all night"* asks nothing. **Naming a card is
not the signal; asking something about one is.** That is the second time this
intuition has been tried and failed — D-15 already withdrew an earlier version
of it — which is worth more than the feature would have been.

**What actually catches the message.** `quantifier` + `second_person` + a loose
operating point. Not knowledge of what a Psyduck is. Pinned in
`test_the_catalog_is_blind_to_what_we_do_not_stock` so the limitation stays
visible, and `rule_intent` was widened so the degraded path can still route it.

---

## B-19 · Choosing the threshold on the test set, nearly

**What happened.** The first Suite A run used threshold 0.5 — which I picked
after reading a sweep over the held-out segment. That is tuning on the test set.
The reported score becomes the best of nineteen tries rather than an estimate of
anything, and holding a segment out buys nothing.

**And the second version was still wrong.** Moving selection to the training set
fixed the threshold but not the *model* choice: when B-18's feature helped train
and hurt test, the obvious move — compare both on test, keep the better — is
model selection on the test set by another name.

**Fix.** Every choice that is not a weight is made by 5-fold cross-validation on
the training segments (`cv_threshold`), the test segment is read once, and the
weights file ships the threshold so the eval **reads** an operating point rather
than choosing one.

**Then F1 turned out to be the wrong objective anyway.** It weights a missed
buyer and a wasted glance equally. The observation says otherwise: at 0.15 msg/s
even a loose threshold puts ~3 items/minute in front of the seller, while a
dropped question is a lot that closes unanswered. So the shipped operating point
is the **lowest threshold whose surfaced rate stays inside operator capacity**
(~3/min, about one per lot) — derived from the observed pace, like the latency
budget, rather than taken from convention. It moved the cutoff 0.59 -> 0.24 and
recall 59.3% -> 88.9% on held-out data.

**Lesson.** Two separate traps, and the second is the subtle one. Not tuning on
the test set means not using it to choose the *model* either. And an operating
point taken from a convention is not derived — F1 encodes an assumption about
relative costs that this product does not have.

---

## B-20 · The same parameter, opposite correct values — and a pipe that hid a crash

**Thinking on the classify path.** D-17 specified "short classification,
`max_tokens: 256`, no thinking" from the start. The code never passed the
parameter, so it inherited adaptive thinking, and the reasoning consumed the
budget before the JSON was emitted: `{"intent":"unkn` and a parse error. It only
appeared once B-19's operating point dropped far enough to escalate real volume.

B-15 **rejected** disabling thinking on the draft path because it cost citation
discipline. Nothing transferred: drafting composes an argument from evidence,
classification picks one label from a closed set with worked examples in the
prompt. **Same parameter, opposite right answer** — which is exactly why neither
should be a default.

**And the process bug, which is worse.** `uv run ... | tail -10` hid a `NameError`
and an exit code of 1 for two full Suite A runs. The fitter crashed before
writing, the eval read a **stale weights file**, and the numbers looked
plausible — a threshold of 0.69 reported against a model fit for 0.59.

**Lesson.** Never read a result through a pipe that can swallow the exit status.
Any harness whose output is a number needs the run to fail loudly, because a
stale artifact and a fresh one are indistinguishable once the traceback is gone.

---

## B-21 · The cross-platform test, and the cascade does not win it

**What was tested.** 28 labelled messages from a different seller on a
**different platform** (eBay Live, `docs/research/observation-ebaylive-2026-09-13.md`),
against a cascade whose weights and threshold were fitted entirely on Whatnot.
Nothing was refitted. The incumbent is *simulated* here — eBay Live has no
platform highlight, so the question-mark rule characterised on Whatnot is
applied rather than observed.

| arm | recall | precision | F1 |
|---|---|---|---:|
| A0 question-mark regex | 66.7% [35-88] | **100.0%** [61-100] | **80.0%** |
| A1 + stage-1 gate | **88.9%** [56-98] | 57.1% [33-79] | 69.6% |
| A2 + classification | 66.7% [35-88] | 85.7% [49-97] | 75.0% |

**The incumbent's F1 beats the full cascade on this show.** Stated plainly
because it is the result. Its precision is perfect here — every question mark in
these 28 messages belonged to a seller-directed question.

**With n=9 positives nothing above is distinguishable from anything else.** The
intervals overlap completely; a single message is eleven points of recall. No
claim in either direction survives this sample size, including the flattering
reading that we nearly matched it.

**What DOES survive, because it is the same measurement at a fourth segment:**

| segment | platform | incumbent recall | A1 gate recall |
|---|---|---:|---:|
| batch0 | whatnot | 40.0% | 90.0% |
| batch1 (held out) | whatnot | 40.7% | 88.9% |
| batch2 | whatnot | 68.8% | 90.6% |
| **show2 (held out)** | **ebaylive** | **66.7%** | **88.9%** |

The incumbent swings **40-69%**. The gate sits at **89-91% across four segments
and two platforms**, on weights it never saw either held-out set of. That is the
stability claim from the chat analysis, and it is the first evidence for it that
is not from the show it was fitted on.

**Where the loss actually is: stage 2, not the gate.** A1 caught 8 of 9 here —
the same recall it gets on Whatnot. A2 then **vetoed two true positives**, which
it never did on the Whatnot test set. The classifier's prior about what counts
as seller-directed is the part that failed to transfer, not the features.

**And one veto is arguably correct labelling, not a model error.**
`nick did you see that galade SAR the tourney promo` was labelled `hype_noise`
by the annotator and surfaced by the cascade. It is a question, addressed to the
seller by name. Whether that is a false positive depends on a judgement call
that two reasonable people make differently — which is itself the finding, and
why the `at_mention`/first-name gap predicted in the observation doc could not
be confirmed here.

**Lesson.** A second dataset is worth more when it refuses to confirm you. The
headline number did not transfer; the *mechanism* claim did, and it is the one
worth defending — the features read intent, and intent does not depend on how
many people in a given twenty minutes happened to press shift-slash.

---

## B-21b · Two corrections to B-21, and one of them undermines its own headline

**First: I reported a single run of a stochastic arm.** A2's veto is a model
call, so it is not deterministic. Five runs on the *unchanged* labels:

| | range over 5 runs | mean |
|---|---|---:|
| A2 recall | 66.7% - 77.8% | 73.3% |
| A2 precision | 85.7% - 87.5% | 86.8% |
| **A2 F1** | **75.0% - 82.4%** | **79.4%** |
| incumbent F1 (deterministic) | — | 80.0% |

B-21 quoted 75.0%, the **bottom of that range**, and concluded the incumbent
won. The mean is 79.4% against the incumbent's 80.0% — indistinguishable. The
conclusion "the incumbent's F1 beats the cascade" was drawn from one sample of a
distribution and should not have been stated that way. This is B-15's lesson
arriving a second time: do not call a comparison from n=1 run.

**Second: one label was revised after seeing the result, and that needs
declaring.** `nick did you see that galade SAR the tourney promo` was labelled
`hype_noise` / not-seller-directed, was the cascade's only false positive, and
the annotator then revised it to `attribute_q` / seller-directed on reflection.

Both scores, so the reader can discount as they see fit:

| labels | positives | A2 F1 (mean of 5) | incumbent F1 |
|---|---:|---:|---:|
| original | 9 | 79.4% | **80.0%** |
| revised | 10 | **86.3%** | 75.0% |

**Why the revision is defensible, and why that is not enough on its own.** The
hypothesis was **pre-registered**: `observation-ebaylive-2026-09-13.md` §5 names
this exact message as evidence that the scorer lacks a first-name-address
feature, and it was committed at 20:32, twenty-five minutes before the data was
labelled (`git log`: 87bd8b0 then ada04af). The label was assigned *against* a
prediction already on record, then corrected toward it.

That is meaningfully better than post-hoc rationalisation, and it is still a
label changed after seeing it cost us a point. **Both numbers are reported and
the original is the one to quote to a sceptic.**

**What must NOT follow.** The obvious next move — add a first-name-address
feature, refit, re-measure on this set — would be tuning on the test set with
extra steps. The feature is recorded as a candidate for a future fit on new
training data, and deliberately not implemented now.

**Lesson.** Pre-registering a prediction is what makes a later correction
legible instead of suspicious. It does not make the correction free — it makes
it *auditable*, which is the most that is available once you have seen the
answer.

---

## B-22 · What generalises, stated as narrowly as the evidence allows

Three claims were available after the cross-platform test. Two do not survive.

**Does not survive: "the cascade beats the incumbent on a new platform."**
A2's mean F1 over five runs is 79.4% against the incumbent's 80.0% on the
original labels. A wash (B-21b).

**Does not survive: "the incumbent is unstable."** Its per-segment recall reads
40.0 / 40.7 / 68.8 / 60.0, which *looks* erratic, but a chi-square test of
homogeneity across the four segments gives **chi2(3) = 5.65, p = 0.097**. It does
not reject. The instability is suggestive and has been quoted in this repo as
though established; at this n it is not, and the claim is withdrawn.

**Survives, and is the claim to make: the gate's recall STRICTLY DOMINATES the
incumbent's on held-out data from two platforms.**

Paired McNemar over the 37 held-out seller-directed messages (batch1 + show2 —
batch0 and batch2 are training data and are excluded):

| | |
|---|---:|
| incumbent recall | 17/37 = 45.9% |
| gate recall | **33/37 = 89.2%** |
| gate catches, incumbent misses | **16** |
| **incumbent catches, gate misses** | **0** |
| McNemar exact | **p = 3.05e-05** |

**Zero discordant pairs in the other direction.** Not "higher recall on average"
— *every* message the regex finds, the gate also finds, plus sixteen more. The
one-sidedness is what makes it significant at n=37.

**Why it is less surprising than it sounds, which has to be said first.** The
gate **subsumes** the incumbent: `question_mark` is one of its fifteen features
and carries the largest weight, +2.481 against a bias of −1.201. A message whose
only active feature is a question mark scores **0.78** against a threshold of
0.24. So dominance is close to structural.

It is not guaranteed, though. Every negative weight summed is −2.664, and a
message carrying a question mark *plus all five negatives at once* scores 0.200
and **fails** the gate. The superset property is therefore an empirical result
about real traffic — no message in 79 positives hit that combination — not an
arithmetic identity.

**So the defensible sentence is:** *adopting the gate is a strict recall
improvement over the platform's current behaviour, with no observed recall
regression, on held-out data from two platforms and two sellers.*

**Everything it does not say.** Nothing about precision, which is worse by
design — the gate is a recall-oriented filter and stage 2 exists to buy the
precision back. Nothing about the full cascade, which is where the eBay Live
loss actually was. Nothing about F1. And the incumbent on show 2 is *simulated*
rather than observed, because eBay Live has no platform highlight.

**Lesson.** The impressive-sounding claim (beats the incumbent) and the
true-sounding one (the incumbent is unstable) both failed. The one that held was
narrower than either and had to be found by asking what test the data could
actually support — a paired test on the same messages, rather than a comparison
of two averages computed from different samples.
