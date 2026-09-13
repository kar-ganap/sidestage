# SideStage — Technical Design Document

A real-time copilot for a solo eBay Live trading-card seller. It watches chat,
decides which messages deserve the seller's attention, drafts replies that are
**verified against assembled evidence before they can be sent**, and proposes
showcase actions through a ledger that records how to undo them.

> **How to read this.** `DECISIONS.md` holds 44 decisions with their rejected
> alternatives. `BUILD-LOG.md` holds 31 entries on what building it taught us —
> mostly things we got wrong. This document is the design those two produced,
> with the measurements that back it. Every number here is reproducible from a
> command in the repo; none is asserted.

---

## 1. The problem, from observation rather than assumption

Two shows were watched and transcribed: a Whatnot rapid-fire slab auction
(~190 viewers, 8–20 s lots, graded stock, $111–410 winning bids) and an eBay
Live raw-singles auction (~63 viewers, 65 s median lots, $1 starts, $1–70
winning bids). **513 chat messages labelled**, on two platforms and two sellers.

Three facts from that corpus shape everything below.

**Chat is a needle-in-haystack problem at low volume, not a firehose.** 0.15
msg/s, and 14% of messages are directed at the seller — 12.0 / 16.8 / 13.3%
across three segments of one show (B-85). The design problem is
precision at low volume, not backpressure. *(This falsified the original
load-shedding justification for the cascade — D-15.)*

**The platform's own triage is a question-mark regex.** Whatnot highlights
messages in its UI; across 485 messages **every** highlighted row contains `?`
and **no** un-highlighted row does. It scores 53.6% recall / 80.4% precision
pooled. So the job is not "detect questions" — it is detecting *intent without
interrogative syntax* (`lugia next!`, `320 for gare plz`, `You got any
psyducks`) while rejecting *chatter that carries a question mark* (`base set?`
asked of another viewer).

**eBay Live shows only three chat messages at a time.** A fourth arrives and the
first is gone. On Whatnot a missed question can be scrolled back to; here it is
destroyed. That makes a triage queue the only durable record of what the room
asked, and it is the strongest argument the field work produced.

---

## 2. Architecture

One FastAPI process serves the API, the operator console and the static files.
All logic is server-side; there is no front-end build step (D-06, D-07).

```mermaid
flowchart TD
    M["chat message"] --> P0

    subgraph TRIAGE ["app/triage.py — which messages does the seller ever see?"]
        P0["0 · prefilter<br/>length, platform events"] --> P1
        P1["1 · scorer<br/>15 interpretable features → p<br/><i>2.7 ms p50</i>"]
        P1 -->|"p &lt; threshold — no model call"| DROP["dropped<br/><i>~84% of real traffic</i>"]
        P1 -->|"p ≥ threshold"| P2["2 · classify<br/>intent · seller_directed · confidence<br/><i>1.8 s · 14–35% of traffic</i>"]
        P2 -->|"confident veto"| DROP
        P2 --> P3["3 · cluster<br/>near-duplicates → one card + a count"] --> P4["4 · rank<br/>intent × demand × recency"]
    end

    P4 --> Q(["operator sees a ranked queue"])
    Q -->|"operator picks a card"| R1

    subgraph LOOP ["app/pipeline.py — the core loop"]
        R1["1 · resolve<br/>what is this about?"] --> R2["2 · assemble<br/><b>everything assertable,<br/>before a word is generated</b>"]
        R2 --> R3["3 · draft<br/>reply + claims citing fact ids"]
        R3 --> R4["4 · verify<br/><b>each claim vs THE FACT IT CITED</b><br/><i>0.9 ms p95 CPU</i>"]
        R4 -->|"all violations repairable<br/>and no retry spent"| R5["5 · repair<br/>one bounded retry"] --> R3
        R4 --> R6["6 · settle"]
    end

    R6 --> PASS["pass / repaired<br/>operator can send"]
    R6 --> BLOCK["blocked<br/>refused text + reason + safe fallback"]

    style DROP fill:#f0f2f5,stroke:#8b97a6
    style BLOCK fill:#f6e6e6,stroke:#b23b3b
    style PASS fill:#e3f2ee,stroke:#0f8a72
    style R2 stroke-width:3px
    style R4 stroke-width:3px
```

**The two bold steps are the design.** Assembling evidence *before* generation is
what turns verification into a dict lookup instead of a network call, and
checking each claim against *the fact it cited* — rather than the strongest fact
available — is what stops a true-but-miscited claim from passing.

`app/pipeline.py` is the file to read first — it is the whole core loop in 180
lines and every other module hangs off it.

### The seam that makes the model swappable

`LLMClient` is a Protocol with two methods (`app/llm.py`). The verifier checks
**output**, not provenance, so "could you swap in a different model?" costs
twenty lines rather than a rewrite (D-18). `ReplayClient` satisfies the same
Protocol from recorded fixtures with no credential, which is simultaneously the
no-key reviewer path and the circuit breaker's degraded mode (D-32) — one
mechanism doing two jobs.

---

## 3. The claim contract — why verification is cheap

This is the central mechanism and the thing the rest of the system is shaped
around.

**Evidence is assembled *before* a word is generated.** `assemble()` fetches
every fact that could legitimately back a reply for this intent and subject, and
hands the model a numbered list. The model must return, alongside its reply, a
list of `Claim`s — each naming the **fact id** that establishes it.

```python
Fact(id="f2", kind=VARIANT, authority=RECORD,
     value={"variants": ["shadowless"]}, source="catalog.items.itm_006")

Claim(type=VARIANT, value="shadowless", source_fact_id="f2",
      quote="shadowless copy")
```

```mermaid
sequenceDiagram
    autonumber
    participant O as Operator
    participant P as pipeline
    participant C as Catalog
    participant M as Model
    participant V as Verifier

    O->>P: question from the queue
    Note over P,C: everything assertable is fetched BEFORE generating
    P->>C: assemble(intent, subject)
    C-->>P: f1 identity, f2 variant, f3 catalog, f4 observational
    P->>M: system prompt + FACTS + question
    M-->>P: reply + claims, each naming a fact id
    Note over P,V: no network call, the facts are already in hand
    P->>V: verify(draft, evidence)
    V-->>P: verdict + violations, 0.9 ms p95 CPU
    alt every violation repairable
        P->>M: one bounded retry with the feedback
    end
    P-->>O: pass / repaired / blocked + reason
```

**The ordering is the mechanism.** The fetch happens before the generation, which
is why the check is a dict lookup rather than a network call. Reverse them —
generate first, then go and check — and verification costs a round trip per claim
and cannot sit on the critical path at all.

**Because the fetch already happened, verification is a dict lookup.** Measured
at **0.9 ms p95 of CPU** (`evals/bench.py`, over all 18 recorded drafts rather
than one synthetic reply). That is not an optimisation detail — it is
what makes verification affordable on the critical path at all, and it is what
makes D-36's precomputation design safe (a cached draft can be re-verified
against fresh evidence at serve time for under a millisecond, and dropped if
the world moved).

### Authority is four-valued, and the fourth is the interesting one

`record` (the listing) · `catalog` (what was ever printed) · `third_party`
(pop reports, comps — time-varying, need an as-of) · **`observational`**.

Observational attributes — centring on a raw card, holo swirl, whitening, edge
wear — exist only in the physical card in the seller's hand. They are
unfalsifiable from here, therefore unverifiable, therefore **never asserted**.

`_observational()` does something more than stay silent: it mints **a fact for
each thing we are forbidden to assert**, so the model declines *while citing a
fact* rather than improvising a hedge the coverage backstop would then block.
Refusal is citable. 7 of 15 catalogue items are raw, and 43 of 89 adversarial
cases target a raw lot.

### Three failure modes the contract creates, and the rules for each

**Mis-citation.** A claim can be *true* while citing a fact that does not
establish it. The verifier checks each claim against **the fact that was cited**,
never the strongest fact available — otherwise the ledger records that we
verified something we did not, which is worse than a blocked reply because it is
invisible (B-04).

**Under-claiming.** One claim whose quote spans two assertions gets one check.
`quote_spans_sentences` is a repairable violation (B-09).

**Uncovered assertions.** Default deny (D-11): any sentence containing a number,
a superlative or a commitment, with no claim over it, blocks. This is why
dropping an unknown claim type is safe — the sentence it was meant to cover
becomes unbacked and blocks on its own merits.

### Repairability decides whether a retry is worth 2.4 seconds

`UNREPAIRABLE` (false against an authority — no rewording makes it true) vs
`REPAIRABLE` (overstated). One bounded retry, and only when **every** violation
is repairable; one unrepairable violation poisons the batch (D-10b).

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Drafted
    Drafted --> Verified : verify()

    Verified --> PASS : no violations and text non-empty
    Verified --> Repairing : all violations REPAIRABLE, no retry spent
    Verified --> BLOCKED : any UNREPAIRABLE violation
    Verified --> BLOCKED : retry already spent

    Repairing --> Drafted : redraft with the feedback
    Drafted --> REPAIRED : second pass clean

    PASS --> [*]
    REPAIRED --> [*]
    BLOCKED --> [*] : operator may still send the fallback

    note right of BLOCKED
        An empty reply is NOT a pass.
        Nothing to verify is not the same
        as nothing wrong: every check asks
        "is this assertion supported", and
        none of them fires on the absence
        of one.
    end note
```

Measured: the repair round fires on **6.2%** of benign traffic and converts
**100%** of what it fires on; without it B2 over-blocking would be 12.3% rather
than 6.2%. It costs nothing at the median and owns the tail (+7.8 s). Kept
inline for that reason (B-16).

---

## 4. Spike 1 — claim verification

**The claim, as originally written:** *reply safety does not depend on the model
behaving, because every assertion is checked against evidence fetched before
generation.*

**The claim, after the ablation:** verification buys safety and costs
responsiveness, both measurably, and **the cost is the better-established
effect**. The original wording is retained above because it is what the design
was built to deliver and the measurement is what narrowed it.

**How it is measured.** Suite B runs the *whole pipeline* on 89 adversarial and
77 benign cases. Adversarial cases carry a chat message and a lot, not a
pre-written reply, which forces the honest question: *did anything unsafe reach
the buyer?* A case can end safely two ways — the model never took the bait, or
the verifier caught it — and both count. Escape is judged by an **independent
`claude-opus-5` grader**, because using the verifier to score itself is circular.

```
B1 adversarial — 89 cases
   answered safely            74   83.1%   model denied correctly, or declined
   blocked by the verifier    11   12.4%
   ESCAPED                     2    2.2%   <- residual risk
   SAFE overall               87   97.8%

B2 control — 77 cases
   OVER-BLOCKED             6-8   7.8-10.4%   <- the number that matters
```

**That 97.8% was unfalsifiable, and it took an adversarial review to say so.**
A system hard-wired to reply *"let me check that one and come right back to
you"* scores **100% safe** here and **0% over-blocked** on B2 — strictly better
than what ships, on both headline numbers. `SAFE_FALLBACK` was deliberately
written to pass the verifier on its merits, so refusing everything is the
literal optimum of this scoreboard. A safety metric whose maximum is silence is
measuring abstention.

It was also **unattributable**: one arm, scored against nothing, so nobody could
say whether a bare model reaches 95% on these cases and the whole apparatus buys
2.8 points.

### The ablation (`evals/run_spike1.py`)

Three arms and a control, scored on **two** questions per reply — *does it
assert the falsehood* (Opus grader, as before) and *does it answer what was
asked* (`app/judge.py`). Only both counts, which is the axis a mute system
fails.

| arm | | |
|---|---|---|
| **S0** bare model | no evidence, no claim contract, no verifier | a competent assistant told who it is |
| **S1** + grounding | evidence assembled, claims required, **no verifier** | |
| **S2** + verification | the shipped pipeline | |

**S1 and S2 share one generation.** The first version ran them as separate
calls, and at n=89 the sampling noise of a stochastic model was larger than the
effect being measured — it reported the verifier catching 4 cases and "missing"
4, where the misses were a different roll of the same dice. `PipelineResult`
exposes `first_text` so the only difference between the arms is the verifier,
which is the entire point of an ablation. S0 cannot be paired: it is a different
prompt, so it is reported from a separate full run.

```
B1, 89 adversarial cases           SAFE   RESPONSIVE    BOTH   blocked
  S0  bare model (sonnet-5)       46.1%       93.3%   43.8%      0%
  S1  + grounding                 94.3%       98.9%   93.3%      0%
  S2  + verification              96.6%       91.0%   85.4%   19.1%

  same, drafting with haiku-4.5
  S1  + grounding                 88.8%       88.8%   79.8%      0%
  S2  + verification              95.5%       79.8%   75.3%   27.0%

  CONTROL — only ever says the safe fallback
                                 100.0%       59.6%   59.6%    100%
```

**What the evidence contract buys is large and unambiguous.** S0 -> S1 is
**+48.2 points of safety**. A bare model asserts the falsehood in over half of
these cases. That is Spike 1's real result and it was never measured before.

**What verification buys is smaller, real, and comes with a bill.** Paired
McNemar over both models (178 case-model pairs, each a trial of the same
question):

| | verifier caught | verifier lost | McNemar exact |
|---|---:|---:|---:|
| safety | 8 | 1 | **p = 0.039** |
| responsiveness | 6 | 21 | **p = 0.006** |

Both effects are real; **the cost is better established than the benefit.** On
the shipped model alone the only significant effect is the cost — 7
responsiveness losses against 0 gains, p = 0.016 — and the 2:0 safety gain does
not reach significance.

**What it catches is specific, which matters more than the aggregate.** Pooled,
the eight catches are `pop_missing_as_of` x4 (a population figure quoted with no
read date), `unbacked_claim` x2, `identity_mismatch`, `authenticity_value_gate`
(promising marketplace authentication on a lot below the $250 gate), and
`variant_not_on_copy`. These are named domain rules firing on real replies that
grounding alone shipped. An aggregate of 96.6 vs 94.3 hides that.

**The safety gain is larger on the weaker model** — 6:1 on Haiku against 2:0 on
Sonnet — which is what a *guarantee* should look like: its value appears when
the model behaves worse. Neither per-model test reaches significance, so this is
a direction the data is consistent with, not a finding.

**Why the suite cannot settle it.** S1 alone reaches 94.3% on Sonnet, so at most
5.7 points are available for any verifier to win and only a handful of cases can
discriminate. Suite B1 is **saturated**, which is the same reason the original
97.8% was unattributable. Measuring this properly needs adversarial cases where
grounding alone fails; that is item 2 of what I would do next.

**The responsiveness axis is judged by `app/judge.py`, which nothing measures.**
Every responsiveness number above inherits that error. Stated here rather than
in a footnote because it is the weakest link in this section.

**B2 is quoted as a range because the arm is stochastic.** Five runs gave 7.8%
four times and 10.4% once. A single run of a model-in-the-loop suite is one
sample of a distribution, and quoting the favourable one is how B-21 reached a
conclusion it had to withdraw.

**B2 is mandatory, not optional** (D-26). A verifier that blocks good replies is
useless whatever its recall, and **B1 structurally cannot see it** — from inside
an adversarial suite, over-blocking and correct-blocking are identical. Every
significant verifier bug in this project was found by B2: cert-read-as-grade,
reserve-inside-comp-range, value gates on queued lots, and B-24 below.

**The 16.9% figure was 41.6% until B-24.** The verifier was blocking replies that
correctly asserted the *absence* of a fact — `_comp`'s own violation message
reads *"Say we do not have enough recent sales rather than giving a number"*, and
it fired on a reply that said exactly that. Twenty-two adversarial cases moved
from *blocked* to *answered safely* when it was fixed, with no change in escapes.
B1 had been scoring the bug as evidence the verifier worked.

**Verifier registry** — a dict of pure functions, one per claim type, which is
why adding a check is a function rather than a branch. It must be **total**: a
lookup returning `None` used to mean *skip*, and `identity` and `sizing` were
offered to the model with no entry, so a claim of either type went entirely
unchecked while still satisfying the coverage backstop (B-37). `identity` is the
second most common claim type in the recorded fixtures. It now fails closed, and
`tools/check_docs.py` fails if this block drifts from the code again:

```python
REGISTRY: dict[ClaimType, Verifier] = {
    IDENTITY: _identity, SIZING: _sizing, VARIANT: _variant,
    COMP: _comp, POP: _pop, GRADE: _grade, CONDITION: _condition,
    CENTERING: _centering, AVAILABILITY: _availability, PRICE: _price,
    BID: _bid, SHIPPING: _policy, RETURNS: _policy, AUTHENTICITY: _policy,
}
```

---

## 5. Spike 2 — the triage cascade

**The claim:** a cheap interpretable gate plus a narrow model escalation reads
*intent* rather than punctuation, and the two-stage structure earns its keep.

**Design.** Fifteen hand-designed features, one per failure mode observed in real
chat — `catalog_entity` because `You got any psyducks` names stock and asks
nothing syntactic; `at_mention` because `cross_user` is 141 of 485 messages and
the biggest false-positive source. **Only the weights are fit**, by ~25 lines of
gradient descent with no sklearn; scoring is a pure-Python dot product so the
runtime needs no numpy. Every drop carries the features that moved it, which is
the property an embedding score cannot provide and the reason the console can
show *why* a message was dropped.

**Method, stated because it went wrong first.** Weights are fit on 676 messages
(352 synthetic + two real Whatnot segments). **Everything that is not a weight —
which features, which threshold — is chosen by 5-fold cross-validation on
training data**, after an early version read the threshold off a sweep over the
held-out set (B-19). The operating point is then *derived* rather than
F1-optimal: at 0.15 msg/s even a loose threshold puts ~3 items/min in front of
the seller, while a dropped question is a lot that closes unanswered, so the
shipped cutoff is the loosest threshold inside operator queue capacity.

### Results — the ablation is the point

Held-out Whatnot segment (161 messages, 27 seller-directed):

| arm | recall | precision | F1 |
|---|---|---|---:|
| A0 question-mark regex *(the incumbent)* | 40.7% | 68.8% | 51.2% |
| A1 + stage-1 gate | 88.9% | 42.9% | 57.8% |
| A2 + classification | 77.8% | 91.3% | 84.0% |

**The architecture claim, on 189 held-out messages from *two platforms*:**

```
A1 gate alone     P 47.1%  R 89.2%  F1 61.7%    37 false positives
A2 gate + model   P 93.6%  R 79.3%  F1 85.8%     2 false positives

stage 2 removes 35/37 false positives (95%), costing 3-4 true positives
McNemar on errors: p < 0.0001
```

That is the claim worth defending, because it needs no baseline: the gate trades
precision for recall by design, and the model buys it back. **Neither arm alone
does both**, which is the argument for a cascade rather than one classifier.

### Cross-platform: what generalises and what does not

The eBay Live set (28 messages, 10 seller-directed) was labelled independently
and nothing was refitted.

**Does not survive:** "the cascade beats the incumbent on a new platform." A2's
mean F1 over five runs is 79.4% against 80.0% for a bare `?` rule on the
original labels. A wash (B-21b).

**And the word "incumbent" does not belong in that sentence** (B-84). eBay Live
has **no visible highlight at all** — the observation record states that
`highlighted` cannot be read off there, so every row in `triage_show2.jsonl`
carries `highlighted: false` as an *absence marker*, not as an observation that
the platform declined. The arm it was compared against is therefore a naive `?`
baseline, not a product. On Whatnot the same arm genuinely is the incumbent,
because `highlighted` and `"?" in text` agree on **485 of 485** observed
messages — but that justification does not travel, and the function is now named
`arm_question_mark` so it cannot be read as travelling.

**Does not survive:** "the incumbent is unstable." Per-segment recall reads
40.0 / 40.7 / 68.8 / 60.0, which looks erratic, but chi-square homogeneity over
four segments gives **p = 0.097**. It does not reject. Withdrawn — and note that
the fourth of those segments is the eBay Live `?` baseline rather than an
incumbent, so the test was pooling two different things even to reach a null.

**Survives:** paired McNemar over the 37 held-out seller-directed messages from
both platforms — the gate catches **16** the regex misses and the regex catches
**0** the gate misses, **p = 3.05e-05**. Strict dominance on recall.

> Stated with the caveat that makes it honest: the gate **subsumes** the
> incumbent — `question_mark` is one of its fifteen features with the largest
> weight (+2.48 against a bias of −1.20), so a message whose only active feature
> is a question mark scores 0.78 against a 0.24 threshold. Dominance is close to
> structural. It is not guaranteed: all negative weights sum to −2.66, and a
> question-mark message carrying all five negatives scores 0.200 and fails. The
> superset property is an empirical result about real traffic, not arithmetic.

---

## 6. Evaluation

Five suites (D-26). A, B and the bench are built; C, D and E are specified with
data ready.

| suite | what it proves | result |
|---|---|---|
| **A** triage vs incumbent | reads intent, not punctuation | gate recall 88.9%, cascade precision 93.6% on held-out data from two platforms |
| **B1** adversarial guardrails | nothing unsafe reaches the buyer | 96.6% safe against 46.1% for a bare model (ablated, §4) |
| **B2** false-positive control | the verifier is calibrated, not paranoid | 9.1% over-blocked; **7.9-point responsiveness cost, p = 0.016** |
| **C** grounding & abstention | asks "which Mew?" exactly when it should | 36/36 curated; **77% under chat noise, and every loss is a silence, not a wrong card** |
| **D** unit + golden replay | deterministic, CI-safe, no key | 199 fixtures; the tape raises on prompt drift |
| **E** moment detection | hot/stalled/normal on 10 real labelled lots | 10/10 — **and so do 215 other threshold pairs** |

**Train on synthetic, test on real**, with `triage_test.jsonl` never fit or tuned
against. Every file carries `source`, so the two can never be silently mixed.

**Honest limits of the eval.** `shipping_returns_q` and `buy_commit` are **0 of
513** — no number is quotable for either (see §9). Cluster compression is correct
behaviour but not a headline: 161 real messages contained **two** repeats. And
the real training segments share a show and a seller with one held-out set, so
that split measures generalisation across time within a broadcast; only the eBay
Live set is genuinely cross-seller.

---

## 7. Performance and cost — measured, not targeted

`evals/bench.py`, p50/p95/p99 rather than medians, because the interesting
failures live in the tail.

| path | p50 | **p95** | p99 | target |
|---|---:|---:|---:|---|
| triage stage 1 (gate) | 2.7 | **27.0** | 43.8 | ≤ 50 ms ✅ |
| evidence assemble | 0.1 | **0.1** | 0.7 | — |
| **verify (claims vs facts)** | 0.1 | **0.2** | 0.2 | — |
| triage stage 2 (escalated) | 1763 | **2317** | 2317 | ~~600 ms~~ → 8 s ✅ |
| draft → first readable token | 2229 | **6834** | 6834 | ≤ 1500 ms ❌ |
| draft → sendable (verified) | 3897 | **9954** | 9954 | ≤ 3000 ms ❌ |

**The draft budgets are missed and stay missed.** Two things make that honest
rather than evasive. First, the 1.5 s figure was derived for the **nudge** path —
a 5 s auction reset minus 3 s of human reading — and the draft path inherited it
without re-derivation; nothing binds the draft path at 1.5 s (B-14). Second, the
derivation assumed the operator's reading time was *serial* with generation.
Streaming makes it parallel, which is why the path now carries two budgets split
at the first readable token.

**Two latency optimisations were measured and rejected**, both because they cost
precision the control set could see:

- *Disable thinking on the draft path* — saves 520 ms, raises over-blocking from
  9.2% to 13.3% while safety stays flat. Thinking is buying **citation
  discipline**, which is Spike 1's mechanism (B-15).
- *Take the repair round off the critical path* — it halves over-blocking and
  fires on 6.2% of traffic, so it costs nothing at the median (B-16).

**Model tiering is inverted from intuition.** Sonnet is **2.8× cheaper than
Haiku** for triage, because Haiku's minimum cacheable prefix sits above our
2,229-token prompt so it pays full list on every call. Price per token is the
wrong unit; price per call *after cache eligibility* is the right one, and
eligibility is a step function (B-03, D-17b). Measured $0.00146/call.

---

## 8. Failure modes and degradation

**Circuit breaker → fixtures, not errors.** After three consecutive live
failures the client flips to `ReplayClient` (D-32). A reviewer gets a working
console rather than a stack trace, and degraded mode is the *same code path* as
the no-key path, so it is exercised constantly rather than only in incidents.

```mermaid
flowchart LR
    REQ["draft / classify"] --> CB{"breaker<br/>tripped?"}
    CB -->|no| LIVE["AnthropicClient<br/><i>live call</i>"]
    LIVE -->|success| OUT["LLMResult"]
    LIVE -->|"failure"| FC["count it"]
    FC -->|"3 consecutive"| TRIP["breaker trips"]
    FC -->|"fewer"| FB
    TRIP --> FB
    CB -->|yes| FB["ReplayClient<br/><i>recorded fixtures</i>"]
    FB --> OUT
    NOKEY["reviewer with no<br/>ANTHROPIC_API_KEY"] --> FB

    style FB fill:#e3f2ee,stroke:#0f8a72,stroke-width:3px
    style TRIP fill:#f6e6e6,stroke:#b23b3b
```

**`ReplayClient` is reached two ways, and that is the point.** The path a
reviewer takes with no API key is the *same code* as the path a production
incident takes. A degraded mode exercised only during incidents is a degraded
mode nobody has tested; this one runs on every clone.


**Triage fails open.** If the classifier is unavailable or returns nothing
parsable, a message that passed the gate is **surfaced** with a rule-derived
intent, never dropped. Dropping a buyer is silent — no error, no log line the
operator reads, and the aggregate barely moves — so it gets a test rather than a
comment (`test_classifier_failure_surfaces_rather_than_drops`).

**Blocked is a product surface, not an error.** The operator sees the refused
text, the violation beside it, and a safe alternative they can send without
retyping (D-23). They can still send — with the fallback or their own words —
journalled as an override. Refusing would make the verifier a gatekeeper over a
human; it exists to stop the *machine* asserting something unbacked.

**Automation ladder.** L0 observe · L1 suggest (default) · L2 auto-send · L3
auto-send + reversible action · L4 never in v1. Promotion is earned per intent
class, on measured accept-unedited rate. **The ladder changes who presses send;
it never changes whether verification happens.**

---

## 9. What we got wrong

`BUILD-LOG.md` has 25 entries. These are the ones that changed the design.

**B-01 · The headline demo was silently broken.** The two-Mew abstention — "which
Mew?" — never fired, because the resolver indexed on full card names. Nothing
errored; it returned a confident, plausible answer. A feature whose failure mode
is *plausible output* cannot be verified by looking at output.

**B-03 · The cheap model was the expensive one.** See §7.

**B-15 / B-16 · Two latency optimisations, both refused by the control set.** An
optimisation argued from a stopwatch has to be priced against B2, because B2 is
the only suite that can see what it broke.

**B-19 · I nearly tuned on the test set, twice.** First by reading a threshold off
a held-out sweep; then, subtler, by proposing to choose between two feature sets
on their test scores. Not tuning on the test set means not using it to choose the
*model* either.

**B-20 · The same parameter has opposite correct values on two calls.** Thinking
off is right for classification (a label from a closed set) and wrong for
drafting (composing an argument from evidence). Neither should have been a
default. *Also: a `| tail` pipe hid a `NameError` and an exit code, and two eval
runs reported numbers from a stale weights file.*

**B-21b · I reported one run of a stochastic arm** and concluded the incumbent
won. Five runs put it at a wash.

**B-24 · The verifier blocked its own prescribed answer.** See §4. The most
instructive bug in the project: it was invisible to the adversarial suite *and*
the adversarial suite was reporting it as a success.

**B-25 / B-28 · A defensive default is a silent assertion.** Twice. `_dt(None)`
returned `datetime.now()`, so fourteen of fifteen lots — five of them *fixed-price
shop listings* — were told to the model as auctions closing shortly. And
`getattr(lot, "extensions", 0)` read a field the loader never populated, which
would have shipped the entire nudge layer inert while looking healthy. Where a
type already says `| None`, the loader's job is to preserve that; where a field
is ours to populate, the crash is the feature.

**B-27 · The inverse of a markdown is not a markdown.** `markdown` refuses any
price at or above the current one, so D-04's four actions contain no way to raise
a price — and the ledger was journalling a compensating action that could never
execute. The fix is commercial rather than technical: a markdown is a public
commitment, and restoring the price is a second decision, not a rewind.

**B-30 · Nobody types the apostrophe.** `Champion's Path` normalised to
`champion s path`; viewers type `champions path`. The set was unreachable from
the only surface form that occurs in real chat. A normaliser has to converge on
the form users produce, not one that is merely consistent.

---

## 10. Known limitations

**~~B-13~~ — closed by B-32, and the residual is the honest part.** Claim-level
verification checks what a reply *asserts*, not what it *implies*. The judge now
ships (`app/judge.py`), run against the operator's reading time rather than
before it — p50 2.0 s, p95 2.1 s, so it is usually free on the path that
matters. It is **advisory**: a warning beside send, never a block, because a
judge can only say *this reads as unresponsive*, which is a judgement rather
than a finding. It degrades open.

What remains a limitation is that **nothing measures whether the judge is
right.** It is scored by no suite of its own; `evals/run_spike1.py` uses it as
the responsiveness axis, which makes it a measuring instrument that is itself
unmeasured. That is the real B-13 residual.

**Two intent classes have zero instances in 513 messages.**
`shipping_returns_q` — because both platforms answer it in a persistent banner
(`$5.00 Flat` on eBay Live), so the question is pre-empted rather than
unimportant. This emptied D-22's designated first rung on the automation ladder;
`availability_q` replaces it, on measured volume.
`buy_commit` — because in an auction you do not type "I'll take it", you **bid**,
and the bid goes through the platform UI where chat cannot see it. D-13 billed
this class as "where GMV comes from"; that was wrong for a read-only-auction
persona. The purchase signal lives in `system_event` instead.

**Sampling censors the chat rate.** Frames at 5–15 s against a 3-message window
means messages are lost when several arrive inside one window. Every rate here is
a **lower bound**.

**The mock marketplace is not real eBay semantics.** `MockMarketplaceAdapter`
implements 6 fault modes including lost responses and partial writes; real API
integration was out of scope (D-24). The adapter records a result under its
idempotency key *before* rolling the lost-response fault, which is the ordering a
real client needs.

**Suite E rests on ten lots and one stalled instance, and quantifies its own
powerlessness.** `216 of 2,500` threshold pairs score the same 10/10 —
extensions anywhere in 1..6 crossed with movement anywhere in 1%..42% — and that
count is printed next to the score by default, because a `10/10` standing alone
reads as validation however carefully a docstring qualifies it (B-81). Observed
extension counts are 1, 2, 3, 3, 6, 7, 7, 11, 15, 24 and hot starts at 43%
movement against 12% for the highest normal one, so any cutoff in either gap
splits the same two groups. The suite validates the *shape* of the rule — that a
gap exists and that extension count finds it — not the value of the constants
(B-29). Constraining them needs lots inside the gap: 4–5 extensions at 15–40%
movement. None were observed across two shows.

**Suite C was saturated, and now has an arm that can fail.** 36/36 on every
curated axis is what an eval written by whoever wrote the resolver looks like.
The same cases under realistic chat noise — dropped apostrophes, lost spacing,
transpositions, where the expected answer is unchanged — score **77%** (B-82).
The rate is less interesting than the direction: **of the 27 it loses, 0 resolve
to the wrong item and 27 find nothing.** It degrades to silence rather than to
confident error, which is the property D-14 exists to protect and is now
measured rather than assumed. Lost spacing is the weakest mode (61%) and is
deliberately unpatched — matching across word boundaries is the change most
likely to turn safe silences into confident errors, and this arm is what would
have to prove it did not.

**The triage scorer has no feature for being addressed by name.** eBay Live
viewers write `nick did you see that galade SAR`; `at_mention` is a strong
*negative* because on Whatnot an `@handle` meant one viewer answering another.
Deliberately not fixed: the gap was found on a held-out set, so closing it needs
new training data rather than a refit against the set that revealed it.

---

## 11. What I would do next

1. **Give the judge a suite of its own.** It closed B-13 (B-32) and is now the
   responsiveness axis of Spike 1's ablation — a measuring instrument that
   nothing measures. Labelling responsive/unresponsive on the recorded corpus
   and scoring it against a second grader is the obvious next step, and until
   that exists every responsiveness number in this document inherits its error.
2. **Harder adversarial cases for Suite B1.** The ablation showed the suite is
   saturated: grounding alone reaches the high nineties, so only a handful of
   cases can discriminate and the suite has no power to measure what
   verification adds. That is why the original 97.8% was never attributable.
   The suite needs cases where grounding alone fails.
3. ~~**D-36 precomputation**~~ — *measured and abandoned (B-31).* The design was
   to draft and verify the top-k (lot × intent) pairs off the queue lookahead.
   The hit rate on recorded traffic is **4%**, which does not pay for the
   complexity. The reasoning that made it attractive still holds and is worth
   recording: verification is a sub-millisecond dict lookup, so a cached draft
   can be re-verified against fresh evidence at serve time and dropped if the
   world moved. Kept here as a rejected option rather than deleted, because the
   argument for it was sound and only the measurement killed it.
4. **More labelled chat from a third seller.** Every generalisation claim here
   rests on two shows; the cross-platform set is 28 messages. A first-name
   feature, a taxonomy class for product commentary that is not market
   commentary, and any recalibration of the operating point all need data that
   does not yet exist.
5. **Real marketplace integration** behind the existing adapter Protocol. The
   ledger, the fault modes and the read-back are all built against a seam that
   was designed for this; the mock is the thing that gets replaced.
