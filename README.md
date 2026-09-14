# SideStage

A real-time seller copilot for **eBay Live** trading-card sellers.

It reads the live chat stream, surfaces only what is worth answering, drafts replies
grounded in the listing, catalog and policy record — and **blocks anything it cannot
prove** before it reaches a buyer.

> A viewer asks what the Champion's Path Charizard would do **if it were a PSA 9**. The
> model answers with the only sales on record — *"for PSA 10 the last 7 sold
> $1,390–$1,690, past 90d"* — and the verifier **blocks it**: the claim cites a
> population report where a grade claim needs a grade record. The reply would have
> quoted a four-figure range for a grade this copy does not have. Caught by domain
> logic, not by a tone filter.
>
> The operator sees *"Let me check that one and come right back to you."* and the draft
> never reaches the buyer. Reproduce it in one minute: [Run it](#run-it), pick
> **Charizard VMAX Champion's Path** in the header's lot selector — the evidence block is
> built around the active lot, and the default is a different Charizard — then ask
> `what would the vmax do if it were a psa 9?`

---

## See it running

**<https://sidestage.fly.dev>** — always on, no sign-in, reseeded from `data/*.json` at
boot, and the Reset button in the console puts it back for the next person.

That instance runs against **live models**, so it generates fresh rather than replaying:
the same question can land on a different verdict run to run, which is the honest
behaviour of the system and is measured (B-21b: one input scored F1 75.0%–82.4% across
live runs). **The scripted walkthrough below is the keyless local one**, because there
the outcomes are recorded and exact — the example above is the recorded verdict, and it
is the same every time.

---

## Run it

```bash
uv sync                                    # Python 3.13, resolved from uv.lock
uv run uvicorn app.main:app --reload       # → http://127.0.0.1:8000
```

**No credential is required.** With `ANTHROPIC_API_KEY` unset the app runs in replay mode
against 335 recorded fixtures and the full workflow still works, deterministically. Set a
key in `.env` (copy `.env.example`) to run against live models. `GET /healthz` reports
which mode you are in, the models, and the latency budgets — never the key.

## Exercise the core workflow

Five calls, in the order the product works. Every one is runnable with no key.

```bash
# 1. Push the REAL recorded show chat through the cascade — including the 84%
#    that is noise and the messages the platform's own highlighter missed.
curl -sX POST localhost:8000/api/replay -H 'content-type: application/json' \
     -d '{"n": 40, "source": "triage_test"}' | jq '.stats'

# 2. See what got surfaced, what got dropped, and the features that decided each.
curl -s localhost:8000/api/state | jq '.queue[0], .log[0]'

# 3. Draft a reply for the top-ranked card: assemble evidence → generate →
#    verify → at most one repair. (Ranking decides which card is first, so take
#    the id from the queue rather than assuming it.)
CARD=$(curl -s localhost:8000/api/state | jq -r '.queue[0].id')
curl -sX POST localhost:8000/api/cards/$CARD/draft | jq '{verdict, reply, violations}'

# 4. A write, with a read-back and a recorded inverse. Add SIDESTAGE_FAULTS=1 to
#    the server and the same call survives lost responses and stale reads.
curl -sX POST localhost:8000/api/actions -H 'content-type: application/json' \
     -d '{"action":"markdown","params":{"lot_id":"lot_s01","new_price":150}}' | jq

# 5. Drive the auction. Bids raise; omitting `amount` extends the timer with
#    nothing behind it, which is what a stall is.
for a in 900 925 950 975 1000 1025 1050 1075 1100; do
  curl -sX POST localhost:8000/api/lot/lot_006/bid -H 'content-type: application/json' \
       -d "{\"amount\": $a}" > /dev/null; done
curl -s localhost:8000/api/state | jq '.nudge'
```

`POST /api/reset` puts everything back, catalog included.

**All five are also clickable in the console**, which is the faster way to see
them: *Replay* fills the queue, picking a card drafts and verifies it, the
**Write path** panel runs step 4 — including a second button aimed one dollar
under the lot's floor, so the refusal is something you can trigger rather than
read about — and **Bid $** / **Timer +** on the live lot drive step 5. The two
auction buttons are separate because a bid must raise the price: if every timer
extension carried one, `stalled` would be unreachable by construction.

## What to read

`app/pipeline.py` is the core loop and the shortest path to understanding the system.
`app/verify.py` is the safety component and where most of the engineering is.
`app/models.py` is the vocabulary — a **Claim** cites a **Fact**; start there if the rest
does not parse.

## The two spikes, with what each is and is not entitled to claim

### Spike 2 — triage cascade (`app/triage.py`, `evals/run_triage.py`)

A 15-feature logistic gate fit by hand-rolled gradient descent, then an LLM
classification pass, then clustering and ranking. The ablation is the point:

| arm | recall | precision | F1 |
|---|---|---|---:|
| A0 question-mark regex *(the incumbent)* | 40.7% | 68.8% | 51.2% |
| A1 + stage-1 gate | 88.9% | 46.2% | 60.8% |
| A2 + classification | 77.8% | 80.8% | 79.2% |

Stage 2's contribution is measured on the 189-message two-platform set, where
the gate emits **33** false positives. See `docs/TDD.md` §5 — the figure this
line carried for most of the project was a subtraction from a population that
does not exist (B-124). **Neither arm alone does both**, which is the argument for a cascade
rather than one classifier.

Two claims were tested on a second platform and **withdrawn**: "the cascade beats the
incumbent on a new platform" (a wash, F1 79.4% vs 80.0%) and "the incumbent is unstable"
(chi-square homogeneity p = 0.130, does not reject). What survives cross-platform is
strict dominance on recall: the gate catches 16 the regex misses, the regex catches 0 the
gate misses, p = 3.05e-05.

### Spike 1 — claim/evidence verification (`app/verify.py`, `evals/run_spike1.py`)

Evidence is assembled *before* generation, so verification is dict lookups over facts
already in hand rather than a second model call. The model emits a reply plus structured
claims, each citing a fact id; each claim is checked against the fact it cites, and a
coverage backstop blocks any assertive sentence no claim is responsible for.

The ablation reports three arms — bare model, + grounding, + verification — scored on
**safety and responsiveness**, against a control that only ever says the safe fallback.
The control exists because on a safety-only metric a system hardwired to say
*"let me check that and come back to you"* scores 100% and beats everything shipped.

```bash
uv run python -m evals.run_spike1                    # both suites, 3 arms
uv run python -m evals.run_spike1 --suite b1 --arms S1,S2
```

See `docs/TDD.md` for the measured numbers, including **what the ablation does not
show**: on this suite verification adds no detectable safety over grounding alone.

## Test and evaluate

```bash
uv run pytest                          # 336 tests, no credential needed
uv run python -m evals.run_guardrails  # Suite B: adversarial + benign
uv run python -m evals.run_triage      # Suite A: the cascade ablation
uv run python -m evals.bench --paths free   # latency, no model calls
uv run python tools/check_buildlog.py  # every B-NN cited in code is written up
uv run python tools/check_docs.py      # every pinned number, against the code
uv run python tools/mutate.py          # 43 mutants: delete a rule, see if a test notices
```

`tools/mutate.py` is the answer to *"how do you know the tests are any good?"* —
it deletes one rule from `app/verify.py` at a time and re-runs the suite. A
mutant that SURVIVES means no test constrains that rule. It also reports
`unverified` — the suite is green and the probe could not confirm the mutant
bit — because a harness that cannot say "I don't know" reports a score it has
not earned (B-125, B-127).

## Documentation

| | |
|---|---|
| [`docs/SUBMISSION.md`](docs/SUBMISSION.md) | **Start here.** The reviewer packet: ten-minute path, what is claimed and what was withdrawn, and the AI-use disclosure. |
| [`docs/PRD.md`](docs/PRD.md) | What it is for, who it is for, and what it refuses to do. |
| [`docs/TDD.md`](docs/TDD.md) | Architecture, the spikes, and the measured results. |
| [`docs/RESULTS.md`](docs/RESULTS.md) | **Every number in one place** — both spikes, latency, the suites, and what did not survive measurement, each with its population and the command that reproduces it. |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every architectural decision, the alternative rejected, and why. |
| [`docs/BUILD-LOG.md`](docs/BUILD-LOG.md) | Every bug worth remembering, including two adversarial passes over the verifier that found 30 defects. |
| [`docs/DOMAIN_PRIMER.md`](docs/DOMAIN_PRIMER.md) | How trading cards work, and what makes a claim about one verifiable. §7 is the verifier registry. |
| [`docs/research/`](docs/research/) | Field observation instrument and notes. |

## Layout

```
app/
  pipeline.py    the core loop — read this first
  verify.py      claims vs facts; the safety component
  models.py      the vocabulary — a CLAIM cites a FACT
  triage.py      the cascade: prefilter → gate → classify → cluster → rank
  evidence.py    everything assertable, fetched before a word is generated
  moments.py     auction dynamics → the one-line nudge
  actions/       marketplace adapter (mock, six fault modes) + write ledger
  main.py        FastAPI: REST, static console
data/            seeded catalog, set records, comps, policies (modeled)
static/          the operator console — no build step (DECISIONS.md D-07)
fixtures/        335 recorded LLM responses for replay mode
evals/           the suites; `run_spike1.py` is the Spike 1 ablation
```

## A note on the data

The seeded catalog is **modeled from public collector references**. It is not licensed
eBay or TCGplayer data, and the marketplace adapter is a mock with deliberately realistic
failure semantics rather than a live integration. Both are stated plainly in the TDD's
known limitations.
