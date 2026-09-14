# Results

Every measured number in one place, with the population it was measured on and
the command that reproduces it.

**How to read this page.** Three rules, and they exist because breaking them is
how this project's worst documentation bugs happened:

1. **Every number carries its population.** `49.4%` and `40.7%` are both real
   and neither is "the answer" — they are different suites on different data.
   A figure without its denominator is not a result.
2. **A range beats a point** wherever a model is in the loop. Arms with a model
   are reported over three runs, low–high, because a single run of a stochastic
   system is an anecdote.
3. **What did not survive is listed first**, below, because a reviewer should be
   able to find the failures faster than the successes.

Numbers here are pinned by `tools/check_docs.py`, which reads the recorded run
files and fails when a document drifts from them. Run it: `uv run python
tools/check_docs.py`.

---

## What did not survive measurement

These were claimed at some point and removed. They are the most useful part of
this page.

| claim | why it went |
|---|---|
| *"verification makes the system safer"* | **not demonstrated.** On the adversarial suite, verification adds no detectable safety over grounding alone — see §1 |
| *"the cascade beats the incumbent on a new platform"* | a wash — F1 79.4% vs 80.0% on eBay Live (B-21b) |
| *"the incumbent is unstable"* | chi-square homogeneity over four segments, **p = 0.130**, and the test was never licensed (min expected cell 4.56 < 5). Does not reject |
| *"97.8% safe"* as a headline | unfalsifiable — a system that only ever says the safe fallback scores **100%** on it, and the MUTE row below proves it |
| *"verification is a 0.2 ms dict lookup"* | measured on one synthetic draft. **1.0 ms p95 CPU** over the real corpus, ~5× the pre-rewrite figure |
| Suite E's `10/10` as validation | **216 of 2,500** threshold pairs also score 10/10 |
| Suite C's `36/36` as robustness | **77%** under realistic chat noise |

---

## 1 · Spike 1 — does verification actually help?

**The question.** A grounded model that cites its evidence is already fairly
safe. Does a separate verification pass on top of that buy anything?

**The design.** Four arms over the same 89 adversarial cases, same model
(`claude-sonnet-5`), three runs.

| arm | what it is |
|---|---|
| **S0** | bare model, no evidence, no verification |
| **S1** | + the evidence contract (facts assembled before generation, claims cite them) |
| **S2** | + verification (every claim checked against the fact it cited) |
| **MUTE** | a control that answers every question with one fixed safe string |

**Safety** = the reply was blocked, or it asserted nothing false. Cases the
grader could not resolve are dropped from the denominator rather than guessed,
so `n` is 89 except where noted. **Responsiveness** = the reply actually
answered the question.

| arm | safe (median of 3) | range | responsive (median) | range |
|---|---|---|---|---|
| S0 | `██████████░░░░░░░░░░` 49.4% | 48.3–49.4 | `███████████████████░` 93.3% | 84.3–95.5 |
| S1 | `███████████████████░` 96.6% | 95.5–97.8 | `████████████████████` 97.8% | 94.4–97.8 |
| S2 | `███████████████████░` 96.6% | 95.5–97.8 | `██████████████████░░` 92.1% | 87.6–92.1 |
| MUTE | `████████████████████` 100.0% | 100.0–100.0 | `████████████░░░░░░░░` 59.6% | 58.4–61.8 |

*S0's safety denominator is 89, 89, 87 across the three runs; S1's is 89, 88, 89.*

### The finding, stated against my own interest

**S0 → S1 is the whole effect.** Grounding takes safety from ~49% to ~97%. That
is the evidence contract doing the work, not the verifier.

**S1 → S2 is inside the noise.** Per run, the safety delta is **+0.0, +1.2,
−1.1 points** — *the sign is not stable*. Paired across all three runs:

| comparison | discordant pairs | exact McNemar p | verdict |
|---|---|---|---|
| S1 vs S2, **safety** | b=2, c=2 | **1.0000** | no effect detectable; only 4 discordant pairs — underpowered |
| S1 vs S2, **responsiveness** | b=19, c=3 | **0.0009** | S2 is **less** responsive, and this one is real |

**So verification, on this suite, costs ~6 points of responsiveness and buys no
measurable safety.** That is the honest result and it is not the one I wanted.

### Why the component is still in the product

Three reasons, none of which is "the eval said so":

- **The suite cannot see the failure it prevents.** 89 adversarial cases drawn
  from what I could imagine. The verifier's guarantee is *structural* — every
  claim cites a fact of the type that can support it — and the value of that
  shows up on the attack nobody enumerated, which by construction is not in the
  suite.
- **S1's safety is the model choosing to behave.** S2's is a property of the
  code. Those are worth different amounts even when they score the same, and
  they come apart under a model swap: on `haiku-4-5`, S1→S2 safety was **+6.7
  points** (88.8% → 95.5%).
- **MUTE is the control that makes the point.** It is 100% safe and 59.6%
  responsive. Any safety number quoted without a responsiveness number beside it
  can be achieved by saying nothing.

### The MUTE arm, used as an instrument

MUTE sends the *same fixed string* every run, so every recorded run was also a
free test–retest study of the grader. Grading identical inputs, the judge
disagreed with itself on about a fifth of cases and was not unanimous on about a
third. **The measuring instrument has more variance than several of the effects
being measured** — which is the real reason the S1→S2 safety delta is reported
as "inside the noise" rather than as a small positive.

```bash
uv run python -m evals.report_spike1      # every recorded run, and the spread
uv run python -m evals.run_judge          # Suite F: the judge as an instrument
```

---

## 2 · Spike 2 — the triage cascade

**The question.** A live chat is mostly noise. What reaches the seller?

**The design.** Three arms on the same 189 held-out messages from **two**
platforms, gate threshold `0.23`, fit on 609 training rows that are never tested
against.

| arm | what it is |
|---|---|
| **A0** | the incumbent-style baseline: regex / punctuation heuristics |
| **A1** | a 15-feature logistic gate, hand-fit by gradient descent |
| **A2** | the full cascade — gate, then an LLM classifier on what survives |

| arm | precision | recall | F1 | tp | fp | fn |
|---|---|---|---|---|---|---|
| A0 | `███████████████░░░░░` 77.3% | `█████████░░░░░░░░░░░` 45.9% | 57.6% | 17 | 5 | 20 |
| A1 | `██████████░░░░░░░░░░` 50.0% | `██████████████████░░` 89.2% | 64.1% | 33 | 33 | 4 |
| A2 | `██████████████████░░` 87.5% | `███████████████░░░░░` 75.7% | 81.2% | 28 | 4 | 9 |

**A2 has a model in it, so it is measured over repeated runs:** precision
**84.8–87.5%**, F1 **80.0–81.2%**, false positives **4–5**. A0 and A1 are
deterministic and do not move.

### What this shows, and what it does not

- **The gate's job is recall, not precision.** A1 at 50% precision looks bad in
  isolation; it is a *filter feeding a model*, and 33 false positives is the
  price of missing only 4 real questions. Stage 2 removes most of them.
- **Strict dominance on recall.** Cross-platform, the gate catches **16**
  seller-directed messages the regex misses; the regex catches **0** the gate
  misses. Paired, **p = 3.05e-05**.
- **The cascade is not universally better.** On the eBay Live segment alone it
  is a *wash* against the incumbent — F1 79.4% vs 80.0%. That claim was
  withdrawn.
- **Populations differ and it matters.** On the 161-row single-platform segment,
  A0 recall is **40.7%**; pooled over all observed messages it is **53.6%**.
  Same arm, different denominators. Quoting one against the other is the error
  this page's first rule exists to prevent.

```bash
uv run python -m evals.run_triage --both-platforms    # the 189-row table above
uv run python -m evals.fit_triage                     # refit the gate
```

---

## 3 · Latency

From `evals/results/bench.json`, milliseconds, no model calls in any of these
paths.

| stage | n | p50 | p95 | p99 |
|---|---|---|---|---|
| entity resolve | 966 | 3.893 | 35.042 | 66.388 |
| triage stage 1 (gate) | 966 | 3.747 | 36.931 | 67.871 |
| evidence assemble | 1200 | 0.096 | 0.129 | 2.657 |
| **research route** (brief req 4) | 90 | 9.0 | 22.7 | 54.6 |
| **verify — CPU (the work)** | 1188 | 0.638 | **1.016** | 1.201 |
| verify — wall (this machine) | 1188 | 0.668 | 5.695 | 19.556 |

**Read the CPU row, not the wall row.** The wall p99 is ~16× the CPU p99 because
this is a shared laptop; the CPU figure is the work the code actually does. The
number moves between machines, which is why the doc check carries a tolerance
and why the right verbal claim is *"about a millisecond of CPU"* rather than
three significant figures.

**Why it matters architecturally.** Verification is O(1) dict lookups because
the evidence is assembled *before* generation — so checking a claim is a lookup,
not a fetch. That is the reason the safety pass is affordable at all, and the
reason "just call the model twice to check it" was rejected (D-09).

**The research row is the same dividend, collected twice.** The brief asks for
on-demand product research under **2 seconds**. `GET /api/research/{lot_id}`
comes back at **p99 55 ms** — about 36× under budget — because it returns the
assembled *record* (identity, variant, grade, comps with their quotable flag,
pop report, policy, and the operator-only reserve, each with its authority and
as-of) rather than a generated paragraph. No model call, so nothing to stream
and nothing to verify. The target is met by not making the expensive call.

For contrast, the path that *does* generate: end-to-end draft latency on the
adversarial suite is **p50 4,237 ms**, where repairs fire often. D-35 splits
that into time-to-first-token and time-to-sendable and reports the latter at
2.4 s — explicitly *"a truer metric, not a pass."* Neither figure meets 2 s, and
the docs say so rather than quoting the research number as if it covered
drafting.

```bash
uv run python -m evals.bench --paths free
```

---

## 4 · The other suites

| suite | what it proves | result |
|---|---|---|
| **B2** false-positive control | the verifier is calibrated, not paranoid | 9.1% over-blocked on 77 benign cases (range 7.8–10.4% across runs) |
| **C** grounding & abstention | asks *"which Mew?"* exactly when it should | 36/36 curated; **77% under chat noise** — and every loss is a silence, not a wrong card |
| **D** unit + golden replay | deterministic, CI-safe, no credential | 335 fixtures; the tape raises on prompt drift |
| **E** moment detection | hot / stalled / normal on 10 real labelled lots | 10/10 — **and so do 216 of 2,500 threshold pairs** |
| **F** the judge | measures the grader, not the system | ~a fifth disagreement on identical inputs — the bound on every other number here |

**Suite E quantifies its own powerlessness on purpose.** `216 of 2,500`
threshold pairs score the same 10/10, and that count is printed next to the
score by default — a `10/10` standing alone would imply the thresholds were
tuned when the data cannot distinguish them.

**Suite C was saturated until it was given an arm that can fail.** 36/36 on
every curated case meant the suite had stopped carrying information; adding
realistic chat noise — typos, interjections, transpositions, where the expected
answer is unchanged — dropped it to **77%** (B-82).

---

## 5 · Tests, and whether the tests are any good

| | |
|---|---|
| tests | **346**, no credential needed |
| mutation | **44 / 43** mutants killed |
| pinned doc claims | **40**, zero stale |
| recorded fixtures | **335** |
| build-log entries | **105** |

**`tools/mutate.py` is the answer to "how do you know the tests are any good?"**
It deletes one rule from `app/verify.py` at a time and re-runs the suite. A
mutant that **survives** means no test constrains that rule. It also reports
`unverified` — the suite is green and the probe could not confirm the mutant
bit — because a harness that cannot say *"I don't know"* reports a score it has
not earned.

That harness has been wrong three times, and each time something else caught it:
it reported 32/32 while an independent reviewer wrote 19 mutants it had not
defined, of which 15 survived; its adopted list then contained entries that
patched nothing; and the fix for *that* silently dropped 23 mutants from the
denominator. **Every instrument built to catch errors here had to be caught by
something else.**

```bash
uv run pytest                          # 336 tests
uv run python tools/mutate.py          # 43 mutants
uv run python tools/check_docs.py      # 40 pinned claims
```

---

## 6 · Reproduce everything

```bash
uv sync

# free, no credential — these read recorded runs rather than calling models
uv run python -m evals.report_spike1                  # §1
uv run python -m evals.bench --paths free             # §3
uv run pytest                                         # §5
uv run python tools/mutate.py                         # §5
uv run python tools/check_docs.py                     # every number on this page

# these call models and cost money
uv run python -m evals.run_spike1 --runs 3            # §1 from scratch
uv run python -m evals.run_triage --both-platforms    # §2
uv run python -m evals.run_guardrails                 # §4 B1/B2
```

Recorded run files live in `evals/results/`. `check_docs.py` reads those rather
than re-running, which is deliberate: a doc check that costs money and ten
minutes is a doc check nobody runs.
