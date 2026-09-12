# SideStage — Decision Log

Living document. Every entry is a decision we can be asked to defend, with the option we
rejected and why. A diagram without this reasoning tells a reviewer nothing.

Status markers: **settled** · **provisional** (will be revisited once measured) ·
**open** (not yet decided).

---

## Product & scope

### D-01 · The user — settled
**Decision.** One persona: a solo eBay Live trading-card seller who hosts *and* moderates.
~90-minute shows, sequential lots, phone on a ring light, laptop on Seller Hub.

**Why.** eBay is the corporate partner and eBay Live's volume is card-led. A solo seller
has no second pair of hands, which is what makes an operator console a product rather than
a convenience. Every feature must map to a pain this person has.

**Rejected.** A seller + hired moderator team (splits the UX target, and the mod is a
workaround for the problem we're solving). TikTok Shop apparel (better-documented pain, but
misaligned with the partner and far weaker attribute verification).

---

### D-02 · Trading cards primary, small fashion slice — settled
**Decision.** Full domain depth on Pokémon cards; ~20 seeded fashion listings with their
own attribute and sizing verifiers.

**Why.** Card identity is a tuple of discrete checkable attributes with a real authority to
check against (see `DOMAIN_PRIMER.md`). "Does this run big" has no authoritative record,
which would gut the verifier's best demonstration. The fashion slice costs ~2 hours and
answers "does this generalize" with code instead of a paragraph.

**What the slice actually proves** — and it is not "same code, different rows." In apparel,
far more attributes are **observational** rather than record-backed: "does this run big,"
"is the colour true to the photo," "how's the drape." So the fashion slice is where D-12's
authority model does visible work — the copilot defers to the seller far more often in
apparel than in cards, *from the same verifier registry*, because the catalog has less
authority to offer. That is a much stronger generalization argument than watching it answer
a size question.

**Rejected.** Cards only (cleanest story, but generalization becomes a hand-wave).
Fashion primary (lower domain-knowledge risk for the author, much weaker Spike 1).

---

### D-03 · Auctions are read-only — settled
**Decision.** Lots carry `format: bin | auction`. Auction bid state (current bid, reserve,
time remaining) feeds `Evidence` so replies are grounded in it. The copilot never modifies
a bid. Price and stock actions apply to BIN lots only.

**Why.** Grounding in live bid state makes price guardrails sharper — "will you do $400"
is a different answer mid-auction than on a BIN lot — at a fraction of the cost of write
support.

**Rejected.** BIN only (~3h cheaper, but eBay Live is auction-heavy and the omission is
conspicuous). Full auction writes incl. end-early (~6h; genuinely interesting because
ending an auction is irreversible and would stress the ledger, but it risks the core).

**Revisit if.** The core is green well ahead of schedule — end-early is the best
irreversibility case we have.

---

### D-04 · Four write actions — settled, amended 2026-09-12
Push lot to showcase · reorder/swap showcase · markdown a BIN lot (floor-price guard) ·
adjust quantity (oversell guard). These map the brief's "push, swap, markdown, stock
adjustment" onto eBay Live semantics.

**Amendment: consigned stock has a second, harder floor.** Observed live —

> *"They are not mine. I can't take an offer like that when they are not mine. I have been
> instructed to consign them and that is what I am doing."*

A large share of live-sold inventory is **consigned**: the seller does not own it and cannot
price below what the consignor permits. The markdown action assumed the seller controls the
price. On consigned stock they do not, and breaching that floor is a breach of agreement
rather than a bad trade — a categorically different failure from selling below margin.

So lots carry `consigned: bool` and `consignor_floor`, and the markdown precondition treats
the consignor floor as **absolute**: not a warning the operator can override, unlike
`floor_price`. Two floors with different force is exactly the kind of domain rule that has to
come from observation; I would not have invented it.

**Implementation note.** `ConsignorFloorViolation` is a **sibling** of `FloorPriceViolation`,
not a subclass — if it inherited, `except FloorPriceViolation` would silently catch it and the
override path written for the ordinary floor would apply to a floor that must never be
overridden. Both carry `overridable` (True/False) as a machine-readable handle. The check also
keys off `consignor_floor is not None` rather than the `consigned` flag, so a row carrying a
floor without the flag fails closed instead of being priced through.

---

### D-04b · `push_lot` is consequential; `swap_showcase` is reversible — settled 2026-09-12
**Decision.** The two showcase actions sit at different rungs of the ladder.

- **`swap_showcase`** reorders the *queue*. Nothing has happened to any lot; reordering back
  restores the prior state exactly. **Reversible → L3-eligible.**
- **`push_lot`** makes a lot *live*. On a real show the outgoing lot then ends — it sold or it
  didn't, and neither can be undone. **Consequential → capped at L1/L2 with confirmation.**

**Why this needed deciding.** The mock adapter initially demoted the outgoing lot to `queued`
rather than `ended`, which makes `push_lot` look reversible and therefore L3-eligible. That is
a modelling convenience, not reality, and building the ladder on it would put a consequential
action behind a tier that assumes it can be undone.

The real answer is better anyway: **`swap_showcase` is the L3 case**, and D-34's pre-bid
signal ("lot 331 has 6 pre-bids — pull it forward") is usually a *reorder* rather than an
immediate push; you move it up the queue, you don't interrupt a running lot. So the tier that
had nothing in it now holds the action the field observation most directly motivates.

**Stated divergence for the TDD:** the mock leaves the outgoing lot recoverable so the demo
can reset. A real adapter would end it.

---

### D-04c · Both showcase actions are legal on auction lots — settled 2026-09-12
D-03 scopes **price and stock** writes to BIN lots. Pushing or reordering does not touch bid
state, and D-34's headline case is pulling an *auction* lot forward on pre-bid demand — so
reading D-03 as "no writes at all on auctions" would forbid the one action the observation
most clearly motivates.

---

### D-05 · eBay-interest layer is gated — settled
**Decision.** Strategy nudges, top-moment detection, and the session debrief start only
after the demo path, verifier, triage eval, and bench are all done. ~5 hours, around hour 36.

**Why.** These cover three of the four things eBay raised in conversation, and they fall
out of signals the ingestion pipeline already computes — the same stream pays for itself
three times. But they are worthless attached to a core that doesn't run.

---

## Architecture

### D-06 · Python / FastAPI, all logic server-side — settled
**Why.** The AI interview requires reading this code aloud and modifying it live, at
conversational pace, 30 minutes after submitting. Author fluency dominates every other
consideration. A stack I write faster is worthless if it can't be defended.

**Rejected.** TypeScript end-to-end (better real-time UX per hour, one language — but
fluency is the binding constraint here, not velocity).

---

### D-07 · No build step; server-authoritative state — settled
**Decision.** FastAPI serves `static/index.html` plus Preact + htm vendored as a single
file in `static/vendor/`. The server computes the complete console state and pushes it over
one WebSocket; the client is a pure render function that sends back user intents as
commands. No client state machine, no client-side validation, no optimistic updates.

**Why.** Two reasons that reinforce each other. Reviewers get one run command with no node
toolchain and no "did you run the build" failure mode. And guardrails must be authoritative,
which means state lives in exactly one place — Python. The front end has no logic to defend
because it has no logic.

**Rejected.** React + Vite (higher polish ceiling, costs a node toolchain in a Python repo
and ~300 lines of framework code the author didn't write). HTMX (maximally defensible, but
real-time queue updates and keyboard navigation get awkward).

**Constraint.** Hard cap of ~700 lines in `app.js`. Past that we cut UI features, never add
files. `docs/FRONTEND.md` documents the state shape, the six components, and the command
channel; the author reads `app.js` end to end before submitting.

---

### D-08 · SQLite, single instance — settled
**Why.** Zero setup for reviewers, deterministic seeding, transactional ledger.

**Known limitation, stated in the TDD.** SQLite on a volume means one instance and no
horizontal scale. Scale path: ledger and catalog to Postgres, hot catalog index stays
in-process.

---

### D-33 · Reads in memory, writes in SQLite — settled
**Decision.** The catalog (lots, items, set data, comps, policies) is an **in-process dict
index** built from `data/*.json` at boot. SQLite holds only the **append-only side**: the
action ledger, the message log, and the draft/verification log.

**Why the split.** D-09's central claim is that verification is O(1) local lookups rather
than network calls — that is how safety fits inside the 1500 ms draft budget. Routing
evidence assembly through SQL would make that claim false for no benefit at N ≈ 300 lots.
Conversely the ledger genuinely wants what a database gives: ordered, transactional,
queryable append, which is the whole point of an audit trail.

So the answer to "where does state live" has two halves, and each has a reason:
**reads are in-process because they are on the latency critical path; writes are in SQLite
because the audit trail needs transactional ordering.**

**Rejected.** Everything in SQLite (uniform, but puts I/O in the hot path and weakens D-09).
Everything in memory (fast, but the ledger loses ordering guarantees and queryability, and
"auditable" becomes a claim rather than a property).

---

### D-34 · Queue lookahead pre-fetch — settled 2026-09-11, gated behind a green core
**Decision.** Assemble `Evidence` for the next **N upcoming lots** before they go live, and
serve queue and inventory questions from that pre-built snapshot. Refresh when the queue
changes; on fast shows this snapshot takes the second cache breakpoint that D-19's per-lot
layer no longer earns.

**Why — it inverts the latency problem.** Field observation showed lots running 3–10 seconds,
where questions about the current lot are unanswerable by anyone and the real traffic is
*"is X coming up later"* and *"any more Ys"*. The saving grace is that **the lot queue is
known in advance.** Nothing about an upcoming lot has to be fetched at question time.

So *"is the Umbreon coming up?"* answers off a warm snapshot in tens of milliseconds rather
than the ~1500 ms draft budget. **The format that looked like it would break the latency
budget is the one where pre-fetch wins** — the opposite of what the pre-observation design
predicted, and worth saying out loud because it is a genuinely counter-intuitive result.

**Bounded cost.** N lots × evidence assembly, recomputed only on queue mutation, not per
message. The queue is short and changes rarely; this is cheap in exactly the regime where the
per-message path is expensive.

**Gating.** Same rule as D-05 — nothing starts here until the demo path, verifier, triage eval
and bench are green. The primary demo remains a moderate-pace show, because that is what
exercises the brief's mandatory behaviours and what the Champion's Path block needs.

**Upgraded 2026-09-12 — pre-bidding makes the queue signal observed rather than inferred.**
Observed: *"they are all prebid, you can prebid on any of them."* Buyers can bid on lots
**before they go live**, so interest in the queue is measurable in advance. The lookahead
stops being only a latency optimisation and becomes a demand signal:

> **Lot 331 has 6 pre-bids and nothing above it does — pull it forward.**

That is a `push_lot` proposal backed by real bids rather than by chat inference, and it is
the strongest version of this feature. It also pairs with the observed request traffic ("run
the shining dragon", "you will run the stack after the shining?") — the same action,
justified two independent ways.

**Honest note, now partly resolved.** This began resting on a single unscrubbed session. The
2026-09-12 lot log supports it: only ~22% of seller-directed messages referred to the item on
screen; the rest were closed lots, the queue, or the wider catalog. The gating stays, but the
traffic is no longer hypothetical.

---

### D-09 · `Evidence` / `Fact`, assembled before generation — settled
**Decision.** Before any draft is generated, we fetch an `Evidence` snapshot: a list of
`Fact` records, each with `id`, `kind`, `subject`, `value`, `authority`, `source`, `as_of`,
`ttl_s`. The model's prompt states that nothing outside this set is knowable.

**Why.** Three jobs. (1) It bounds the model's universe. (2) It makes verification O(1)
dictionary lookups rather than network calls — *this is how safety fits inside the latency
budget*, because the fetch is already paid for before generation starts. (3) It is the
provenance record: trust chips in the UI and the audit trail in the ledger both read from it.

**Naming.** Earlier draft called this "FactSet." Renamed — it collides with a well-known
financial data vendor, and `Evidence` pairs correctly with the other half of the contract:
a **claim** cites **evidence**.

**Subtlety to build in.** The snapshot goes stale *inside the request*. ~800ms elapses
between fetch and verify, and on an auction lot a bid can land in that window. Live-kind
facts carry a short TTL and are re-read at verify time. Stale evidence is its own violation
type.

---

### D-10 · Claims cite evidence — settled
**Decision.** The model emits structured output `{reply_text, claims: [{type, value,
source_fact_id}]}` via `output_config.format`. Every assertion in the reply must be backed
by a claim, and every claim must cite a `Fact.id` from the evidence snapshot.

**Why.** This is the mechanism the whole system rests on. It converts "is this reply safe?"
— unanswerable — into "does each claim match its cited fact?" — a set of small deterministic
checks. Server-enforced structured output makes the contract a contract rather than a
prompt hope. Assistant prefill is removed on all current models, so this is also the only
way to constrain shape.

---

### D-10b · Violations are typed by repairability — settled
**Decision.** Every `Violation` carries a severity of `UNREPAIRABLE` or `REPAIRABLE`, and the
repair loop only runs for the latter.

**Why.** It is a correctness insight that happens to also be a latency win. A claim that is
*false against an authority* — "1st Edition" on a set that never printed one — cannot be
fixed by rewording; asking the model to try again wastes ~700 ms and then blocks anyway. A
claim that is *unsupported or overstated* — "plenty left" at qty=3 — has a real fact behind
it and one bounded retry genuinely fixes it. Splitting them means the hopeless case fails
fast and the fixable case gets its retry.

**Where it shows up.** The repair rate reported in the D-17 model comparison is only
meaningful over REPAIRABLE violations; an UNREPAIRABLE rate is a measure of how often the
model invents facts, which is a different and more interesting number.

---

### D-11 · Default deny on unenumerated claims — settled
**Decision.** Any sentence containing a number, a superlative, or a commitment verb that
carries no backing claim id at all is blocked, regardless of claim type.

**Why.** The per-type verifiers only cover claim types we thought of. This is the backstop
that makes the system **fail closed** on the ones we didn't. It is also the honest answer to
"what about claim types you didn't enumerate."

---

### D-12 · Record vs observational authority — settled
**Decision.** Every attribute carries `authority: record | catalog | third_party |
observational`. The copilot **never asserts an observational attribute** — centering on a
raw card, holo swirl, print lines, whitening. It defers: "the host will check that on
camera."

**Why.** This comes straight from the domain and it is not a generic safety rule. Some card
attributes live in a record and are checkable; others exist only in the physical card in
the seller's hand. Asserting the second kind is unfalsifiable and therefore unverifiable,
so it is forbidden by construction rather than by prompt.

---

### D-13 · Ten intents plus `unknown` as a route — settled
**Decision.** `attribute_q` · `grade_condition_q` · `price_value_q` · `availability_q` ·
`shipping_returns_q` · `authenticity_q` · `buy_commit` · `negotiation` · `hype_noise` ·
`off_topic_abuse`, plus `unknown`.

`unknown` is a route, not a failure: the question still surfaces as a ranked card, with no
draft. One operator click produces a best-effort draft under wide grounding (full lot record
+ policy corpus), still verified, still backed by D-11.

**Two distinct tails, handled differently.** Tail of *phrasing* — a known intent asked
oddly — is the cascade's job; the LLM escalation catches what the cheap scorer misses. Tail
of *kind* — "do you do consignment?" — is what `unknown` is for. Conflating them leads to
blaming the taxonomy for a scorer problem.

**`buy_commit` routes to an action, not a reply.** "I'll take lot 4" should produce a
proposed write, not a sentence. This is where the ladder meets the write path and where GMV
actually comes from.

**Measured, not assumed.** Every `unknown` is logged with its text; the session debrief
surfaces them; the eval carries a **tail-coverage metric**. Working assumption is 10–15% of
non-noise questions land in `unknown`. If it comes back at 25%, the taxonomy is wrong and
we will know.

**Amended 2026-09-11 after first field observation.** Watching real chat surfaced classes the
armchair taxonomy missed, and two of them are not user messages at all:

- **`system_event`** — platform-generated: "X won", joins, unlocks. Never a question, never
  replied to, but genuinely *signal*: a win is a purchase event and feeds the engagement
  layer.
- **`cross_user`** — viewers addressing each other rather than the seller. These carry
  question marks while not being questions *to us*. Surfacing one is a false positive, and a
  particularly bad-looking one.

Two refinements to existing classes:

- **`hype_noise` includes bare emoji, and emoji are not waste.** 🔥 is noise for the reply
  queue and *signal* for the strategy-nudge and top-moment layer — a burst is the room
  reacting to a specific card at a specific second. Same stream, second use.
- **The entity carries the intent, not the sentence form.** Viewers name cards informally
  inside ordinary sentences — "mew" used mid-sentence. What determines which evidence to
  assemble is the card name, not whether the message is phrased as a question. A message
  naming something currently for sale is high-intent regardless of phrasing. See D-15.

That takes the taxonomy to twelve plus `unknown`, from ten, after 45 minutes of watching.
Worth stating plainly in the PRD: the cost of designing a taxonomy without looking was two
missing classes that are not even user messages.

---

### D-14 · Abstention beats a confident guess — settled
**Decision.** The same principle in three places. Intent: low classifier confidence routes
to `unknown` rather than to the best-guess class. Entity resolution: an ambiguous reference
("the Charizard" with three in the showcase) produces a clarifying question, not a pick.
Reply: an unverifiable claim blocks rather than softens.

**Why.** The risk is asymmetric everywhere. The dangerous intent failure is not "failed to
classify" — it is misclassifying a high-stakes question into a low-stakes class that has
been promoted to auto-send. Same shape for entities and claims.

---

### D-15 · Interpretable triage scorer, not embeddings — settled
**Decision.** Cascade: deterministic lexicon + feature scorer (sub-millisecond) → LLM
escalation for the ambiguous band only → near-duplicate clustering with counts → ranking by
intent × recency × asker value.

**Why.** At 30 msgs/sec we need per-message latency under 5ms, and we need to explain *why*
a message was dropped. A linear model on interpretable features does both. An embedding
similarity score does neither, and adds a heavy dependency that slows reviewer install.

**Amended 2026-09-11 — the cheap arm gains a catalog-entity trie.** Observation showed viewers
naming cards informally inside ordinary sentences ("mew" used mid-sentence). Two things follow,
and the first is the load-bearing one.

**Entity matching is required for grounding regardless.** Whatever the sentence looks like, the
card name is what decides which evidence to assemble — so **a trie over card names, Pokémon
names and set names** has to exist anyway. Building it into the cheap arm costs nothing extra
and yields the intent signal for free: a message naming something currently for sale is
high-intent independent of phrasing. Sub-millisecond, deterministic, and it keeps "why was this
surfaced" answerable in one sentence — *because it named something you are selling* — which is
the property an embedding score cannot give.

*(An earlier version of this amendment argued the trie was needed to rescue bare-noun messages
that syntax-keyed scoring would drop. That rested on a misreading of the observation and is
withdrawn; the grounding argument above is the one that holds.)*

Two cheaper filters run ahead of it, both from the same observation: a **length filter** (the
single-character spam seen in real chat is droppable before any scoring) and a **sender-type
filter** (platform `system_event` messages never enter the scorer at all).

**Rejected.** Sentence-transformers kNN (torch dependency, slow install, opaque decisions).

---

### D-16 · BM25 retrieval with a **pace-aware** referent prior — revised 2026-09-11
**Decision.** Hybrid retrieval over ~300 lots: BM25 plus exact attribute matching, with a
prior on which lot a question probably refers to. **That prior is a function of lot velocity,
not a constant.**

**Why no vector store.** At N ≈ 300, exact attribute matching plus a good prior beats
embedding-only retrieval on the cases that actually matter — ambiguous references — and a
vector store at this scale is ceremony. Rejected sentence-transformers kNN specifically:
torch dependency, slow reviewer install, opaque decisions.

**Why the prior became pace-aware — this was falsified by observation.** The original wording
asserted that "the overwhelming majority of questions refer to the lot currently on screen."
Field observation (`docs/research/observation-2026-09-11.md`) shows that holds only when lots
are slow:

- **Slow lots (1–10 min).** Questions about the item on screen are answerable and common.
  The pinned-lot prior is correct.
- **Fast lots (3–10 s — observed on a Whatnot rapid-fire slab auction).** Questions about the
  item on screen are impossible *for anyone*: a viewer needs 3–5 seconds to type, by which
  time the lot is gone. The observed traffic is *"is X coming up later"* and *"any more Ys"* —
  **the queue and the catalog**, not the active lot.

So the prior shifts mass from the active lot toward the upcoming queue and the wider catalog
as lot velocity rises. The mechanism never changes — resolve entity, assemble evidence,
verify claims — only the default referent does.

**Two observed failure cases the prior must survive**, both seeded into Suite C:

- **Background inventory.** Active lot #021, a viewer asks *"How much for ohtani 👀"* about a
  framed jersey visible *behind the seller*. Sellers keep stock physically in frame and
  buyers ask about what they can see. High buying intent; a naive prior misresolves or drops
  it.
- **Family-level reference.** A bare *"mew"* names a family, not a card — Mew appears across
  dozens of sets and is one keystroke from Mewtwo. Resolution must **abstain** per D-14 and
  ask *"which Mew?"* rather than confidently pick one.

**Consequence.** The prior needs an explicit escape, not merely a weight: when a message names
an entity that does not match the active lot, widen to the full catalog before falling back,
and abstain if the reference stays ambiguous.

---

### D-17 · Model tiering — provisional, pending measurement
| Role | Model | Rationale |
|---|---|---|
| Triage escalation | `claude-haiku-4-5` | Short classification, `max_tokens: 256`, no thinking. (Haiku 4.5 uses `budget_tokens`, not adaptive thinking; `effort` errors on it.) |
| Drafting | `claude-sonnet-5` default | Citation discipline is the gating capability. Haiku 4.5 measured as the alternative. |
| Eval grader | `claude-opus-5`, via Batch API (50%) | Offline; the judge should be stronger than what it judges. |

**The reframe that justifies this.** We are not asking the model to be smart, we are asking
it to be disciplined — and then verifying it anyway. The verifier turns model quality from a
safety question into a latency and cost question. Both models should reach zero
post-verification violations; the difference shows up as **repair rate**, and therefore
latency, not as risk. That table is Spike 2's headline.

**Cost profile is inverted from intuition.** Estimated ~120 drafts/live on Sonnet ≈
$0.50–0.90, but ~800 triage escalations on Haiku ≈ $0.64 — the cheap model costs more,
because volume beats unit price. Argues for keeping the escalation band narrow. Estimate to
be replaced by measured bench numbers.

---

### D-18 · `LLMClient` protocol — settled
**Decision.** A twenty-line protocol with `draft()` and `classify()`, one adapter behind it.

**Why.** The verifier is provider-agnostic *by construction* — it checks output, not the
model. This makes "could you swap in an open-weights model" answerable without having spent
hours on it. Open-weights evaluation is explicitly scoped out: at ~$1.20 per live, cost is
not the binding constraint; latency and citation discipline are. Going multi-provider would
also forfeit server-enforced structured outputs, which is the mechanism D-10 depends on.

**Named future optimization.** If this were worth hours, they'd go to the triage escalation
path — short classification, no citation requirement, and the actual cost driver — not to
drafting.

---

### D-19 · Two caches, two purposes — settled
**Prompt cache** makes the calls we make cheaper and faster. Layout derived from the
workflow, not from a template:

```
[system] role + claim contract (~900) · intent playbooks (~600) · policy corpus (~800)
         ──── breakpoint 1 ────                                        STABLE
[messages] pinned lot record (~400)
         ──── breakpoint 2 ────                        STABLE FOR 2–10 MINUTES
           evidence block + question                               VOLATILE
```

Breakpoint 2 is the interesting one *on a slow show*: the pinned lot doesn't change for
minutes while questions arrive about it, so when the seller advances a lot only that layer
invalidates and the ~2,300-token system prefix survives. The cache hierarchy mirrors the
selling workflow.

**Corrected 2026-09-11 — breakpoint 2 is pace-conditional.** Field observation found lots
running **3 to 10 seconds** on a rapid-fire slab auction, against an assumption of 2–10
minutes. At that pace a per-lot cached prefix never amortises: you get perhaps one question
per lot, so the layer costs a breakpoint and earns nothing.

The system-prefix layer (breakpoint 1) is unaffected and still carries the bulk of the
saving. Breakpoint 2 is therefore enabled only when measured lot dwell exceeds a threshold,
and on fast shows the second breakpoint is spent on the **lookahead queue snapshot** instead
(D-34) — which is where the reusable context actually lives at that pace.

This is the exact measurement trap the observation worksheet warned about: dwell is
format-dependent, and a number read without its format attached would have looked like the
whole cache design was wrong rather than one layer being conditional.

**Answer cache** eliminates calls entirely, keyed on `(intent, sku, price_version,
policy_version, stock_bucket)`. Bucketed stock, not exact — the cache survives small
decrements but invalidates at threshold crossings.

**Commitments.** A test that makes the same call twice and asserts
`cache_read_input_tokens > 0` on the second, to catch silent invalidators (timestamps in
the system prompt, unsorted JSON keys in evidence serialization, varying tool lists).
Verify the prefix clears the model's minimum cacheable floor (512–4096 tokens) with
`count_tokens`. Report **TTFT delta**, not just dollars — the latency win is what buys room
in the 2-second budget. Caches are model-scoped, so Sonnet and Haiku bench runs must not be
interleaved or both look cold.

---

### D-20 · Don't stream drafts — settled
**Decision.** Drafts are generated non-streamed. Only the on-demand research path streams.

**Why.** At p95 ~700ms there is no perceived-latency win, and structured output means
streaming partial JSON — real parsing complexity for nothing. Research outputs run 400+
tokens and genuinely benefit.

**Note.** This supersedes an earlier idea of "stream to the human, gate to the buyer." The
gating principle survives; the streaming half didn't earn its complexity.

---

### D-21 · Action ledger — settled
`propose → precondition snapshot → confirm → execute (idempotency key) → read-back verify →
journal with inverse op`. Compensating actions, not "undo." Read-back divergence triggers
the reconciler.

**Why.** A double-click or a retry must not double-apply a markdown. A marketplace that
returns 500 after partially applying must be detectable. The inverse op is recorded at
journal time, not derived later, because deriving it after the fact requires state we may
no longer have.

---

### D-22 · Automation ladder is per-intent-class — settled
L0 observe · L1 suggest (default) · L2 auto-send · L3 auto-send + reversible auto-action ·
L4 consequential auto-action (never in v1).

Ceilings differ by class. `shipping_returns_q` is highest-volume and lowest-risk and
graduates first. `attribute_q` caps at L1. `negotiation` and `authenticity_q` are
human-gated permanently.

**Promotion is earned, not configured.** A class graduates only after N shadow observations
with an operator accept-unedited rate above threshold and zero guardrail violations. The
UI shows the evidence next to the toggle ("FAQ:shipping — Auto, 98.6% accept over 212
samples").

**The invariant.** The ladder changes who presses send. It never changes whether
verification happens. Nothing skips the verifier.

---

### D-23 · Blocked drafts are visible — settled
**Decision.** A blocked draft is shown to the operator with its violation reason and a safe
fallback, not silently swallowed.

**Why.** Two reasons. It builds operator trust — seeing "I stopped this: claimed 1st
Edition, Champion's Path has no English 1st Edition printing" is what earns permission to
automate later. And it puts the hardest technical decision in the product surface rather
than buried in the stack, which is what the evaluation explicitly asks for.

---

### D-24 · Mock eBay adapter, adversarial by design — settled
**Decision.** `MarketplaceAdapter` protocol with a mock implementation carrying realistic
semantics: rate limits, eventual consistency, partial application, stale reads, injectable
faults. Same shape for `ChatSource`.

**Why.** Real eBay API integration is not achievable in the window. A mock that only ever
succeeds would be a weakness; a mock that reproduces the failure modes the reliability work
exists to handle is the thing that makes the reliability work demonstrable. The adapter
seam is documented against real endpoint shapes so the integration story is concrete.

**Stated plainly in Known Limitations.** This is modeled, not integrated.

---

### D-25 · Fly.io, always-on, deployed on day one — settled
**Why.** A reviewer opens the URL cold, once. Render's free tier spins down after 15
minutes and takes 30–60s to wake — they would conclude it's broken. Fly is ~$2/mo for a
shared-cpu-1x/256MB machine kept warm, Dockerfile-native, with volumes for SQLite.
**Set `auto_stop_machines = false`** — recent `fly launch` defaults to auto-stop, which
reintroduces the exact problem we paid to avoid.

**Process.** Push a hello-world container around hour 4. First-time deploys at hour 46 are
how this goes wrong.

---

## Latency budgets — provisional targets, to be replaced by measured p50/p95/p99

| Path | Target |
|---|---|
| Ingest → triaged and queued (deterministic) | p95 ≤ 50 ms |
| Ingest → triaged and queued (LLM-escalated) | p95 ≤ 600 ms |
| Question card → verified draft on screen | p95 ≤ 1500 ms |
| On-demand research query → answer | p95 ≤ 2000 ms *(the brief's stated target)* |
| Answer-cache hit, end to end | ≤ 80 ms |

**Derived, not asserted — 2026-09-12.** The sub-2-second figure was originally taken from the
brief and justified as "fast is good." Field observation supplies a real derivation.

Auction timers **always reset to less than the base timer**, so the intervention window
*shrinks* as a lot becomes more valuable: roughly 8 s at extension 3, 5 s at extension 15,
with bids landing at 2–3 s. Within one reset window the seller has to read the nudge and
begin speaking:

```
one reset window                    ≈ 5.0 s
human reads it and starts speaking  ≈ 3.0 s
────────────────────────────────────────────
system budget                       ≈ 1.5–2.0 s
```

Same number, earned. Two design constraints fall out of it, and both are load-bearing:

- **The nudge must be glanceable, not readable.** One line absorbed at a glance and spoken
  aloud — `pop 412 · 38 higher` — never a sentence. The auction mechanic dictates the UI
  format.
- **Early detection is mechanically necessary, not merely preferable.** Extension 3 carries
  three times the runway of extension 15, so the detection threshold must be low and the
  pipeline must not eat the window.

---

## Evaluation

### D-26 · Four suites, and B2 is mandatory — settled
**A · Triage.** ~400 labeled messages at realistic class mix (`hype_noise` is 50–60% of
real live chat). Headline is per-class precision/recall and a PR curve — never accuracy,
which is trivially gamed by dropping everything. 70/30 split, thresholds tuned on train,
reported on held-out test. Plus cluster compression ratio and purity.

**B1 · Adversarial guardrails** (~80). Fabricated variant, shadowless on a modern set,
unbacked comp, stock overclaim, invented policy, authenticity overclaim below threshold,
prompt injection via chat, price-anchoring social engineering, grade claim with no cert,
observational-attribute assertion, investment claims.

**B2 · False-positive control** (~60) — **mandatory, not optional**. Benign messages whose
*correct* drafts contain numbers, superlatives and commitments that are properly backed.
A verifier that blocks these is worthless. **Report the over-block rate.** This is the half
that proves the guardrail is calibrated rather than paranoid.

**C · Grounding and abstention.** Ambiguous references. Two metrics: resolution accuracy,
and abstention correctness — did it ask for clarification exactly when it should have.

**D · Unit + golden replay.** Units on claim extractor, each verifier, cluster keys,
ranking, idempotency, ledger inverse ops, and the backpressure drop policy (never shed a
message from a user with an open order). One scenario tape replayed against recorded LLM
fixtures with an asserted event sequence — deterministic, runs in CI with no key, and the
same mechanism powers no-key reviewer mode.

---

### D-27 · Threshold choice is argued from cost asymmetry — settled
A missed high-intent question costs a sale; a false positive costs two seconds of operator
attention. We bias recall hard and publish the operating point with a cost table: at
threshold T, X cards surfaced per hour and Y high-intent questions missed.

---

### D-28 · Harvest during the build — settled
`evals/harvest/` logs every block and every repair as we build. Every time the model
surprises us, that case is promoted into B1. This is the only source of non-synthetic cases
available inside the window.

---

### D-29 · Stated limitations of the evals — settled
Written into the TDD unprompted, because knowing what a test *cannot* prove is the point:

- The adversarial set was authored by the same model family that generates drafts, so it
  under-represents failure modes that family doesn't think of. The harvest set is a partial
  mitigation.
- No evidence about real eBay chat distribution — the tapes are synthetic.
- The verifier covers enumerated claim types; D-11 is the backstop for the rest.
- Nothing here is evidence about GMV or operator load. That requires the pilot.
- The mock marketplace is not real eBay semantics.

---

## User evidence

### D-30 · Observation protocol — settled
**Decision.** Primary evidence is structured observation of a real card show, not
interviews. Screen-record 45–60 minutes, then analyse in two passes: watch once with no
notes, then scrub with the worksheet, pausing to tally.

**Why record rather than observe live.** Tallying, timing lot dwell, and counting missed
questions cannot all be done accurately in real time by someone new to the domain. A
recording makes timings exact (read off the scrubber rather than estimated), makes the
tally pausable, and turns "which 30 minutes should I watch" into a hindsight decision
instead of a live gamble. The recording is also the seed corpus for the tape simulator, so
the thing that de-risks the measurement is an artifact we needed anyway.

**Sampling.** Watch the **middle** of a show. The opening 10–15 minutes is audience-building
— greetings dominate, and sellers deliberately run filler lots to a small audience, so the
mix is unrepresentative. The final 10–15 minutes skews to logistics ("can you combine my
wins"). Prefer a 1–2 hour show: D-01 assumes ~90 minutes because that is roughly what one
person sustains alone, and a four-hour show almost certainly implies a mod or a team —
which is *itself* data about where the persona boundary sits, so note duration either way.

**A tally window is 30 consecutive messages, not 30 seconds.** Consecutive and unskipped, or
the sample undercounts `hype_noise` — the very number being measured. Bounded by message
count rather than time because a busy stream yields 200 messages in a minute (uncountable)
and a quiet one yields 5 (useless): fixing the count holds both effort and sample size
constant, and frees *elapsed time* to become the velocity measurement.

**Precision is not available, and we should not pretend otherwise.** n=30 gives roughly
±18 percentage points at 95% confidence; n=60 gives ±13. So the instrument cannot
distinguish 12% from 19% — it distinguishes *a handful* from *loads*. Every decision it
feeds flips on an order-of-magnitude difference (D-13 at ~25% `unknown`, D-19 at 40 seconds
vs 4 minutes of dwell), which is exactly what a sample this size can resolve. State the
confidence interval in the PRD alongside any number taken from it.

**Variance checks.** Stream B is a second card seller (tests seller variance). Stream C is a
fashion live seller (tests category variance) — the more informative of the two, since it
is the only cheap evidence for the generalization claim in D-02. Both optional.

**Instrument.** `docs/research/observation-worksheet.md`, and a live tally tool with
keyboard entry, an auto-timer and an auto-computed scorecard, persisted to the artifact
database so results can be read straight back into the PRD.

---

## Open

- ~~**O-01 · Anthropic API key**~~ — **resolved.** Key held by the author. It lives in a
  local `.env` (gitignored) and a Fly secret; it is never committed and never appears in the
  access notes. Reviewers need no credential: the deployed instance holds the key
  server-side, and a local run with `ANTHROPIC_API_KEY` unset falls back to recorded
  fixtures automatically.
  **Workload Identity Federation considered and rejected.** Anthropic supports federated
  short-lived tokens (GCP / AWS / Azure / GitHub Actions) instead of static keys, which is
  the right production answer — nothing to leak, tokens expire in minutes. Rejected here for
  three reasons: Fly is not in the federated trust set, so the deployed instance needs a key
  regardless; local development is explicitly outside its scope; and the federation rule and
  service-account setup is real overhead at zero benefit for a single-container prototype.
  Scale path if this became a product: federate on the cloud side, keep a static key only
  for local work and for platforms outside the trust set. (Trap worth noting: a set
  `ANTHROPIC_API_KEY` — even empty — outranks federation, so it cannot be half-adopted.)
- ~~**O-02 · Deploy account**~~ — **resolved.** Fly account in place. See D-31.
- **O-03 · Real seller contact** — worth attempting during the window (card seller Discords,
  r/pkmntcgtrades, eBay Live seller communities). Thin user evidence is not a scored gap,
  but real evidence is cheap leverage on the PRD.

---

### D-31 · Ephemeral database, seeded at boot — settled
**Decision.** No Fly volume. SQLite lives in the container filesystem and is rebuilt from
versioned JSON in `data/` every time the app starts. The action ledger is per-session.

**Why.** This started as a simplification and turned out to be the better product decision:
every reviewer lands on **identical, deterministic state**, so the demo path behaves the
same on every visit and a previous reviewer's markdown can't confuse the next one. It also
removes the volume, which removes the only thing making D-08's single-instance constraint
actually bite.

**Known limitation, stated in the TDD.** Ledger history does not survive a redeploy. Real
persistence is a volume or Postgres; the seam is the same either way because everything
writes through `db.py`.

---

### D-32 · Budget guard and graceful degradation to replay — settled
**Decision.** Two guards on every LLM-backed path: a per-session rate limit, and a circuit
breaker that flips the process into **replay mode** after N consecutive API failures or any
billing/quota error. The degraded state is visible — a banner in the console and a field in
`/healthz` — never silent.

**Why, first reason: the deployed demo spends real money on every visitor.** The prototype
URL is reachable by anyone holding the link, and each draft is a live API call. A reviewer
working the demo path costs cents; a leaked link or a client stuck in a retry loop does not.
The authoritative control is a workspace spend cap on the key itself; these guards are
defence in depth, and they keep the prototype *answering* rather than erroring when the cap
is reached.

**Why, second reason: it is a real failure path a reviewer can trigger.** The evaluation
asks for "a concrete failure path" by name. This one is honest — the dependency genuinely
can fail, the degradation is observable, the fallback still completes the core workflow, and
recovery is automatic. It is a better story than an injected fault because it is a failure
the system will actually meet in production.

**Consequence for the access notes.** Replay mode is not only the no-key path for reviewers;
it is also the system's own failure mode. One mechanism, two jobs — which is why fixtures
are recorded for the whole demo tape rather than a token subset.
