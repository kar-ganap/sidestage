# SideStage — reviewer packet

A real-time copilot for a solo **eBay Live** trading-card seller. It reads the
live chat, surfaces only what is worth answering, drafts replies grounded in the
listing and policy record, and **blocks anything it cannot prove** before it
reaches a buyer.

Three days, one person. Python 3.13 / FastAPI / Preact, no build step, runs
with no API key.

---

## Ten minutes, if that is all you have

```bash
uv sync && uv run uvicorn app.main:app --reload     # → http://127.0.0.1:8000
```

No credential needed — with `ANTHROPIC_API_KEY` unset it replays 335 recorded
fixtures and the whole workflow still works, deterministically.

**Run it this way for the walkthrough below.** The outcomes named in step 1 are
the recorded ones and they are exact. A deployed instance with a key calls the
model live, and the same question can land on a different verdict run to run —
B-21b measured one input scoring F1 75.0%–82.4% across live runs. That variance
is the honest behaviour of the system, not a defect, but it is why the scripted
demo is the keyless one: I can tell you in advance what it will do.

1. **The demo case**, both halves of it. Put the **Champion's Path Charizard**
   up with the lot selector in the header — the evidence block is built around
   the active lot, so it decides what the answer is about.
   - Ask *"what would the vmax do if it were a psa 9?"* The model answers the
     hypothetical with the PSA 10 comps, and the verifier **blocks** it:
     `mis_citation — f5 is a pop fact, a grade claim must cite a grade fact`.
     The claim cited the wrong *kind* of record, which the per-type registry
     catches by construction rather than by recognising the wording. The
     operator sees the fallback; the draft never reaches the buyer.
   - Ask *"is the champions path zard 1st edition"* for the other half. The
     model denies the false premise correctly and cites the set catalog, and it
     **passes**. Blocking is the minority outcome by design — B1 answers safely
     far more often than it blocks — and a copilot that only ever blocked would
     be useless. **Domain logic, and register on top of it**: five overclaim
     rules fire even on well-cited claims (investment advice, "plenty left",
     bare comps, authenticity, observational), and four register rules enforce
     what the prompt only asks for — no exclamations, emoji, greetings or
     sign-offs (B-136).
2. **The workflow**, five calls, all keyless — README §*Exercise the core
   workflow*. Replay real recorded chat, see what was dropped **and why**,
   draft, write with a read-back, drive an auction into a nudge.
3. **The one file to read** is [`app/pipeline.py`](../app/pipeline.py). Six
   steps, and the only judgement in it is small.

---

## What is being claimed, and what is not

*Full tables, populations and reproduce commands: [`RESULTS.md`](RESULTS.md).*

This project's strongest property is that **numbers that did not survive
measurement were removed rather than softened.** A reviewer should be able to
find those faster than the successes, so they are listed first.

### Withdrawn

| claim | why it went |
|---|---|
| *"the cascade beats the incumbent on a new platform"* | a wash — F1 79.4% vs 80.0% on eBay Live (B-21b) |
| *"the incumbent is unstable"* | chi-square homogeneity over four segments, p = 0.130 — and the test was never licensed (min expected cell 4.56). Does not reject |
| *"97.8% safe"* as a headline | unfalsifiable: a system that only ever says the safe fallback scores 100% on it |
| *"verification is a 0.2 ms dict lookup"* | measured on one synthetic draft; it is 1.0 ms p95 CPU over the real corpus, ~5x what it was before the B-56 rewrite |
| Suite E's `10/10 correct` as validation | 216 of 2,500 threshold pairs also score 10/10 |
| Suite C's `36/36` as robustness | 77% under realistic chat noise |

### Held

- **Spike 2 (triage).** On the 189-message two-platform set the gate emits
  **33** false positives, and stage 2 removes most of them — the exact figure
  is measured over three runs because that arm has a model in it, and is in
  `docs/TDD.md` §5 rather than repeated here (B-124). Cross-platform, the gate catches
  16 seller-directed messages the regex misses and the regex catches 0 the gate
  misses, p = 3.05e-05. Strict dominance on recall.
- **Spike 1 (verification).** Ablated three ways, over **three runs**. A bare
  model is **49.4%** safe on 89 adversarial cases; adding the evidence contract
  takes it to **96.6%**; verification adds **nothing measurable** — the safety
  delta is +0.0, +1.2, -1.1, so the sign is not stable, and paired it is 2 vs 2
  discordant at p = 1.00. It costs **5.6–6.7 points of responsiveness in every
  run** (paired p = 0.0009).
  **So the defensible claim is not that verification makes replies safer.** It
  is a guarantee — nothing unbacked ships regardless of how the model behaved —
  and this suite, which is saturated at 96.6% without it, cannot measure that.

---

## The four things this is graded on

### 1 · A working core workflow

`POST /api/replay` → `GET /api/state` → `POST /api/cards/{id}/draft` →
`POST /api/actions` → `POST /api/lot/{id}/bid`. Every one runs with no
credential, and [`tests/test_readme_workflow.py`](../tests/test_readme_workflow.py)
executes the documented sequence so the entry point cannot rot.

### 2 · Engineering floor

**Real domain logic.** A variant claim is checked against what the *set* ever
printed, language-scoped; a centring claim needs a grader subgrade; a comp needs
`n ≥ 5` within 90 days, grade-matched, rendered as a range. See
[`DOMAIN_PRIMER.md §7`](DOMAIN_PRIMER.md), which doubles as the verifier spec.

**A concrete failure path.** [`app/actions/`](../app/actions/) — propose →
confirm → execute → read-back → journal, with idempotency keys and inverses
recorded at journal time. Run the server with `SIDESTAGE_FAULTS=1`: fifteen
writes give 12 verified, 3 diverged, 4 idempotent replays off 4 lost responses.
A divergent read-back is reported and **never retried**, because retrying a
write that may have landed is how you double-apply.

**The registry is a guarantee; coverage is a heuristic.** A claim is checked
against the fact it cites and no exemption exists — four adversarial waves
never broke `_require_kind`. The coverage backstop catches numbers,
superlatives and an enumerated list of commitment verbs, so its recall is the
size of that list; every adversarial finding in this project landed there, and
`_coverage`'s docstring states the bound.

**Focused tests and evals.** **351 tests**, no credential. Five eval suites, each
reporting what it *cannot* establish. `tools/check_docs.py` fails if a number
quoted in the docs no longer reproduces; `tools/check_buildlog.py` fails if a
`B-NN` cited in the source has no write-up.

### 3 · Two technical spikes

[`TDD.md §4`](TDD.md) (verification) and [`§5`](TDD.md) (triage cascade). Both
ablated; both with a stated bound on what the data supports.

### 4 · PRD and TDD that match the implementation

They did not, and a checker now enforces it. See **AI use** below — the drift
is part of that story.

---

## AI use — disclosure

**This was built with Claude (Claude Code), and heavily.** Code, docs and evals
were written in a pair-programming loop. Stating that plainly matters more than
the ratio, because the interesting part is what it did *not* do.

**Where it helped most:** volume and consistency — six eval suites, 306 tests,
and a build log of 95 entries are more bookkeeping than one person sustains in
three days.

**Where it actively hurt, and this is the part worth reading.** Claude wrote a
verifier rewrite that introduced a **prompt-injection path into the safety
component**: the buyer's question was folded into the exemption set, so a buyer
typing *"is it a psa 10?"* made *"this Charizard is a PSA 10"* assertable
against a PSA 9 record. It also wrote docstrings describing guards that did not
exist, an experiment whose paired statistical test ran on **unpaired data**, and
a `10/10` eval headline that could not fail.

**What caught those:** adversarial review, run as a separate pass with the
explicit instruction to break the thing — **five waves**, and each wave found its worst defects inside the previous wave's fixes
— plus mutation testing, which found **11 mutants surviving all 53 tests** of
the file those tests were written for. Both are recorded in
[`BUILD-LOG.md`](BUILD-LOG.md) B-42, B-56 and B-70.

**The working method, stated as a claim I will defend:** an LLM writing code is
fast and confidently wrong in ways that read as careful. The counter is not
review-by-reading — the bugs above all survived reading — it is an adversary
with a budget and a harness that can fail. Every number in this repo that
survived did so because something tried to kill it.

---

## Known limitations

- **n is small and said so everywhere.** Two shows, two sellers, 485 + 28
  messages. Every generalisation claim carries its population.
- **Suite B1 is saturated.** Grounding alone reaches 96.6%, leaving 3.4 points
  of headroom, so the suite cannot measure what verification adds. This is why
  the original 97.8% was never attributable.
- **The responsiveness judge is now measured, and the result bounds everything
  else.** Suite F (`evals/run_judge.py`, B-89) turned the ablation's MUTE arm —
  the same fixed string against the same 89 questions, three runs on disk — into
  a test-retest study that was already paid for. The judge **disagrees with
  itself on roughly a fifth of identical inputs**. Its prior validation was 12
  cases at 12/12, whose Wilson 95% interval is [75.7%, 100%] — consistent with a
  judge wrong a quarter of the time. This is still the weakest link in the
  evaluation; the difference is that it now has a number instead of a shrug, and
  that number is why the S1→S2 safety delta is reported as inside the noise.
- **One adversarial finding is open**, stated rather than papered over:
  coverage is per-sentence, so two occurrences of the same number in one
  sentence are indistinguishable. `"Orders ship within 2 business days, and we
  have 2 of these left"` passes on a cited shipping clause. Closing it needs
  parsing, not matching.
- **The catalog is modelled** from public collector references, not licensed
  eBay or TCGplayer data, and the marketplace adapter is a mock.
- **The deployed demo has one shared session.** The console is a single seller's
  cockpit: chat log, queue and ledger live in one process with no per-visitor
  isolation (D-33), and `fly.toml` pins the app to one machine because splitting
  that state would make the queue empty at random. Correct for the product,
  wrong for a link several people open at once — two simultaneous visitors see
  each other's session and Reset clears it for both. Running it locally avoids
  this and is the deterministic path regardless.

---

## Credit where it is due

The idea of shipping a **single reviewer-facing packet with an explicit AI-use
disclosure** came from noticing that [Papercusp/sidestage](https://github.com/Papercusp/sidestage)
— an unrelated project that arrived at the same name for the same problem —
ships a `docs/submission.md` described as *"the reviewer-facing submission
packet, walkthrough, and AI-use disclosure."* I had five documents and no front
door, and no disclosure at all.

Nothing here is taken from that repository: its contents were not read, the
stack is unrelated (TypeScript monorepo, native mobile apps, streaming gateway),
and the substance of this file is specific to this project. What was borrowed is
the *shape* — that a reviewer deserves one entry point, and that how a thing was
built should be stated rather than inferred. Both were good ideas and neither
was mine.

---

## Map

| | |
|---|---|
| [`README.md`](../README.md) | run it, exercise it, both spikes in brief |
| [`PRD.md`](PRD.md) | who it is for, what it refuses to do, the metrics |
| [`TDD.md`](TDD.md) | architecture, the spikes, the measured results |
| [`DECISIONS.md`](DECISIONS.md) | 44 decisions, each with the alternative rejected |
| [`BUILD-LOG.md`](BUILD-LOG.md) | 110 entries. Every bug worth remembering |
| [`DOMAIN_PRIMER.md`](DOMAIN_PRIMER.md) | how trading cards work; §7 is the verifier spec |
