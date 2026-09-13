"""The model seam: what goes in, what must come back, and what happens when it doesn't.

Three things live here and they are separable on purpose.

**The claim contract.** `DraftOutput` is a Pydantic model passed to
`messages.parse()`, so the shape is enforced server-side rather than requested in
prose. That is what makes D-10 a contract instead of a hope — the model cannot
return a reply without claims, and cannot return a claim without a fact id.

**The provider seam.** `LLMClient` is a Protocol with two methods. The verifier
checks *output*, so it does not care what produced it — which is the honest
answer to "could you swap in an open-weights model?" and the reason that
question costs twenty lines rather than a rewrite (D-18).

**Degradation.** `ReplayClient` serves recorded fixtures with no credential, and
the circuit breaker flips to it after repeated live failures (D-32). One
mechanism, two jobs: the no-key path for reviewers *is* the production failure
mode, which is why fixtures cover the whole demo tape rather than a token subset.

Prompt layout follows D-19 — stable system prefix first behind a cache
breakpoint, the pinned lot behind a second, volatile evidence last. Nothing in
the cached region may contain a timestamp, a uuid, or an unsorted dict, or the
prefix changes every call and the cache silently never hits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.config import FIXTURE_DIR, settings
from app.models import ClaimType, Evidence, Intent

log = logging.getLogger("sidestage.llm")


# =====================================================================
# The claim contract — enforced, not requested
# =====================================================================


class ClaimOut(BaseModel):
    """One assertion, and the fact that backs it.

    `value` is a string even for numbers. A permissive JSON-schema union buys
    nothing here — the verifier has to parse and range-check per claim type
    anyway, and a loose schema is one more place for the model to be creative.
    """

    type: str = Field(description="one of the claim types listed in the system prompt")
    value: str = Field(description="the asserted value, as written in the reply")
    source_fact_id: str = Field(description="the id of the fact that proves this, e.g. f3")
    quote: str = Field(description="the exact substring of reply_text this claim backs")


class DraftOutput(BaseModel):
    """What the drafting call must return.

    `needs_clarification` exists so abstention is a first-class outcome rather
    than something inferred from an empty claim list — a model that wants to ask
    a question should say so, not produce a reply with nothing behind it.
    """

    reply_text: str = Field(description="what would be posted to chat, buyer-facing")
    claims: list[ClaimOut] = Field(description="one per assertion; may be empty only if the reply asserts nothing")
    needs_clarification: bool = Field(default=False, description="true if the reference is ambiguous and the room must be asked")


class TriageOutput(BaseModel):
    """The escalation arm's answer. Small on purpose — this runs on the cheap
    model, on ~10-20% of messages, and volume makes it the real cost driver."""

    intent: str
    referent: str
    seller_directed: bool
    confidence: float = Field(ge=0.0, le=1.0)


# =====================================================================
# Result envelope
# =====================================================================


@dataclass
class LLMResult:
    """Everything the bench and the ledger need, alongside the answer."""

    output: Any
    model: str
    latency_ms: int
    ttft_ms: int = 0
    """Time to the first token of `reply_text`, when the call was streamed.

    Kept apart from `latency_ms` because the two answer different questions.
    `latency_ms` is when the draft can be *sent* — it needs every claim, because
    verification is what gates the send. `ttft_ms` is when the operator can start
    *reading*, and the operator reading overlaps the model still writing.

    The budget derivation in DECISIONS.md subtracts three seconds of human
    reading from the window as though it happened after generation finished.
    Streaming is what makes that subtraction honest. 0 means the call was not
    streamed (replay, or the classify path) — not that the first token was free.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    replayed: bool = False
    fixture_key: str = ""

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_tokens > 0

    def cost_usd(self, rates: "Rates | None" = None) -> float:
        """Per-call cost in USD.

        Rates are passed in rather than defaulted, because a default is where a
        provider assumption hides. `RATES` below is a lookup by model id, and a
        model it does not know returns 0.0 rather than silently pricing someone
        else's tokens at Anthropic's numbers.
        """
        r = rates or RATES.get(self.model)
        if r is None:
            return 0.0
        return (
            self.input_tokens * r.input
            + self.cache_read_tokens * r.input * r.cache_read_multiplier
            + self.cache_write_tokens * r.input * r.cache_write_multiplier
            + self.output_tokens * r.output
        ) / 1_000_000


@dataclass(frozen=True)
class Rates:
    """Per-million-token pricing. Cache multipliers are provider behaviour, not
    universal — another provider may not have prompt caching at all, in which
    case both multipliers are 1.0 and the field simply never fires."""

    input: float
    output: float
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25


# Keyed by model id, so adding a provider is adding rows rather than editing
# arithmetic. Current as of 2026-09; see docs/DECISIONS.md D-17.
RATES: dict[str, Rates] = {
    "claude-sonnet-5": Rates(2.0, 10.0),
    "claude-haiku-4-5": Rates(1.0, 5.0),
    "claude-opus-5": Rates(5.0, 25.0),
}


# =====================================================================
# The stable system prefix — cached, and therefore frozen
# =====================================================================

_CLAIM_TYPES = ", ".join(sorted(c.value for c in ClaimType))

DRAFT_SYSTEM = f"""\
You are drafting a short reply for a live trading-card seller to post in chat \
during an auction. Buyers are reading it in seconds, and the seller may read it \
aloud on camera.

THE ONLY THINGS YOU KNOW ARE THE FACTS YOU ARE GIVEN.
You receive a numbered list of facts. Nothing outside that list is knowable to \
you — not from training, not from inference, not from what is obviously true. \
If a fact is not in the list, you cannot say it.

FOR EVERY ASSERTION, EMIT A CLAIM.
Each claim names its type, its value, the id of the fact that proves it, and the \
exact substring of your reply that it backs. A sentence containing a number, a \
superlative, or a commitment with no claim behind it will be blocked.

Claim types: {_CLAIM_TYPES}

RULES THAT OVERRIDE EVERYTHING ELSE:

1. OBSERVATIONAL ATTRIBUTES ARE NEVER ASSERTED. Condition of a raw card's back \
or surface, centring without a grader subgrade, holo swirl, whitening, print \
lines — these exist only in the card in the seller's hand. When asked, say the \
host will check on camera, and cite the observational fact.

2. NEVER QUOTE A BARE PRICE FROM COMPS. A comp fact carries `quotable` and a \
`phrase`. If quotable, use the phrase verbatim — it is a range with its sample \
size. If not quotable, do not give a number at all; say you do not have enough \
recent sales to quote.

3. A RESERVE IS NEVER SAID OUT LOUD. Facts marked operator_only exist so the \
seller can see them. Repeating one to the room destroys their position.

4. IF A FACT SAYS THE REFERENCE IS AMBIGUOUS, ASK WHICH. Do not pick. Set \
needs_clarification and put the question in reply_text.

5. POLICY COMES FROM POLICY FACTS. Never state a returns, shipping or \
authenticity rule that is not in the fact list, however standard it sounds.

STYLE: one or two sentences. Plain, warm, no exclamation marks, no emoji. Do not \
greet, do not sign off. Write what a busy seller would type with one hand.
"""

TRIAGE_SYSTEM = """\
Classify one live-chat message from a trading-card auction stream.

`seller_directed` is true ONLY when the message asks the SELLER for something — \
information, an action, or an item. It is false for messages addressed to other \
viewers (even with a question mark), for commentary about prices, for banter, and \
for platform events.

Most traffic is noise. In a measured sample of 477 real messages from one show: \
42% hype and banter, 29% viewers talking to each other, 13% market commentary, \
and only 17% directed at the seller at all. Do not over-assign `seller_directed` \
— a false positive costs the seller a glance they cannot spare, and the platform's \
own question-detector already over-fires at 20%.

PUNCTUATION IS NOT THE SIGNAL. Roughly half of genuine requests carry no question \
mark. The platform's built-in detector keys on "?" and scores only 54% recall \
because of it; beating that means reading intent, not syntax.

WHAT NAMES A CARD IS USUALLY A REQUEST. A message naming something the seller \
might have — however misspelled, nicknamed or described — is high intent whatever \
its grammar.

INTENTS

  request            asks the seller to DO something: run a lot, re-run a lot,
                     show the back, go faster, pull something forward
  availability_q     "do you have X", "any X", stock or catalog questions
  attribute_q        what a card IS: set, print run, variant, 1st edition
  grade_condition_q  condition, centring, cert number, "is it clean"
  price_value_q      what something sold for, what it is worth
  shipping_returns_q shipping, combined shipping, returns, invoices
  authenticity_q     is it real, is it authenticated
  negotiation        an offer, a counter, "would you do X"
  buy_commit         "I'll take it", "sold", claiming an item
  market_comment     a VIEWER supplying a price or population figure
  cross_user         addressed to another viewer, not the seller
  system_event       platform-generated: wins, raids, tier unlocks
  hype_noise         reactions, jokes, greetings, single words
  off_topic_abuse    spam, advertising, abuse
  unknown            genuinely unclear — the correct answer when unsure

REFERENTS
  current_lot · upcoming_queue · catalog · closed_lot · my_order · none · ambiguous

WORKED EXAMPLES, all verbatim from real chat:

  "Any fossil psa cards my guy"     -> availability_q · catalog · seller · 0.95
  "Run the shining dragon"          -> request · upcoming_queue · seller · 0.9
      no question mark, unmistakable intent
  "whens the slong zard runnin"     -> request · upcoming_queue · seller · 0.85
      no question mark, a nickname and a misspelling — still a request
  "im no terrorist could you run that beautiful dragonite?"
                                  -> request · upcoming_queue · seller · 0.9
  "Do you have any psyduck card now that I have his phone w and money lol"
                                  -> availability_q · catalog · seller · 0.9
  "Back again plz"                  -> request · current_lot · seller · 0.9
      asking the host to show the back of the card
  "What are these silver boarders out of ??"
                                    -> attribute_q · current_lot · seller · 0.9
  "That shining Mewtwo sell?"       -> price_value_q · closed_lot · seller · 0.8
      a late arrival asking what a finished lot sold for
  "Any chance you have a celebi or suicune wotc black star promo ?"
                                    -> availability_q · catalog · seller · 0.95
      misspelled, and about stock visible behind the seller
  "Lugia clean?"                    -> grade_condition_q · upcoming_queue · seller · 0.9
  "10k all gold stars you got a number in hand?"
                                  -> negotiation · upcoming_queue · seller · 0.85

  "Mines have old backs"            -> cross_user · catalog · NOT seller · 0.8
      a viewer describing their OWN copy; nothing is asked of the seller
  "That was like non 1st ed price"  -> market_comment · closed_lot · NOT seller · 0.85
      commentary with a question mark; nothing is being asked of the seller
  "What the heck is with these non snuggly pokermuns"
                                  -> hype_noise · none · NOT seller · 0.9
  "Steelix nicer than my suv lmao"  -> hype_noise · current_lot · NOT seller · 0.85
      names the card on screen and asks nothing
  "Zard is hanging"                 -> hype_noise · ambiguous · NOT seller · 0.7
      a remark about a lot in progress, not a question
  "841 in 8"                        -> market_comment · current_lot · NOT seller · 0.9
  "13.5k in a 10"                   -> market_comment · current_lot · NOT seller · 0.9
      a viewer quoting grade-indexed prices
  "Back again plz"                  -> request · current_lot · seller · 0.75
      asking the host to show the back of the card again
  "💰$1 STARTS💰🔪WE ARE LIVE🔪"      -> system_event · none · NOT seller · 1.0
  "is raiding with a party of 36."  -> system_event · none · NOT seller · 1.0
  "Oops"                            -> hype_noise · none · NOT seller · 1.0
  "I have this in Japanese"         -> cross_user · current_lot · NOT seller · 0.7
      a viewer talking about their own copy, to the room

HARDER CASES, also verbatim, where the obvious reading is wrong:

  "Can you run the pop series ray"  -> request · upcoming_queue · seller · 0.9
      "ray" is Rayquaza; nicknames and clippings are normal here
  "im no terrorist could you run that beautiful dragonite?"
                                    -> request · upcoming_queue · seller · 0.95
      banter wrapped around a real request; the request is what matters
  "which rayquaza is that in the back?"
                                    -> availability_q · catalog · seller · 0.85
      about stock visible BEHIND the seller, not the lot being sold
  "What charizard is that back there"
                                    -> availability_q · catalog · seller · 0.8
      same case, no question mark
  "Would you do 6k for all your gold star cards on pre bid so we can close it up ?"
                                    -> negotiation · catalog · seller · 0.95
      a bulk offer; never auto-answer one of these
  "That looks kind of not real my guy"
                                    -> authenticity_q · current_lot · seller · 0.8
      authenticity raised as a statement rather than a question
  "Mines have old backs"            -> cross_user · none · NOT seller · 0.75
      a viewer describing their own copy
  "@flatsixgt3 check sold tab"      -> cross_user · none · NOT seller · 0.9
      a mod answering another viewer; nothing is asked of the seller
  "Pika is clean"                   -> market_comment · current_lot · NOT seller · 0.7
      a viewer's condition assessment, not a question
  "dragonite?"                      -> availability_q · catalog · seller · 0.8
      a bare entity plus a question mark is still a real ask
  "Zard is hanging"                 -> hype_noise · ambiguous · NOT seller · 0.75
      names a card but asks for nothing — naming alone is not a request
  "Chromed out"                     -> hype_noise · current_lot · NOT seller · 0.85
  "this bid is so low i feel like im on ketamine"
                                    -> market_comment · current_lot · NOT seller · 0.7
      commentary on price, in the local idiom
  "with honors"                     -> hype_noise · none · NOT seller · 0.9
  "Rigged?"                         -> hype_noise · none · NOT seller · 0.7
      a question mark on a complaint is not a request
  "No swirl??"                      -> grade_condition_q · current_lot · seller · 0.85
      asks about an attribute only the host can see — still a real question
  "not celebrations :)"             -> cross_user · catalog · NOT seller · 0.85
      a viewer ANSWERING another viewer's earlier question
  "you cleared a stack tonight"     -> market_comment · none · seller · 0.7
      addressed to the host, worth surfacing, asks nothing

NOTE ON THE LAST TWO PATTERNS. Viewers routinely answer and correct each other. \
Those messages are addressed to the room, not to the seller, and classifying them \
as questions would surface work that is already done.

`confidence` is your own calibration. Below 0.6 the message routes to `unknown` \
and is surfaced without a draft — which is the correct outcome for anything you \
are not sure about. Abstaining is cheap; a confident wrong answer is not.
"""


# =====================================================================
# Protocol
# =====================================================================


class LLMClient(Protocol):
    """The seam. Twenty lines, and it is the whole answer to "could you swap in
    a different model?" — because the verifier checks output, not provenance.

    `on_text` receives incremental text as it is generated, so a console can
    render the reply while the claims are still decoding. It is deliberately a
    plain `str -> None` callback rather than anything resembling a provider's
    stream event: a client that cannot stream calls it once with the finished
    text, and callers cannot tell the difference. Nothing above this line knows
    that streaming exists.
    """

    def draft(self, *, question: str, evidence: Evidence, intent: Intent,
              repair: str | None = None,
              on_text: Callable[[str], None] | None = None) -> LLMResult: ...

    def classify(self, *, message: str) -> LLMResult: ...


# =====================================================================
# Prompt assembly
# =====================================================================


def _evidence_block(ev: Evidence) -> str:
    """Facts as a numbered list. Sorted by id so the serialisation is stable —
    an unsorted dict is one of the classic silent cache invalidators."""
    lines = []
    for f in ev.facts:
        val = json.dumps(f.value, sort_keys=True, default=str)
        lines.append(f"[{f.id}] {f.kind.value} ({f.authority.value}) — {f.note}\n"
                     f"      value: {val}\n      source: {f.source}")
    return "\n".join(lines)


def _draft_messages(question: str, ev: Evidence, intent: Intent,
                    repair: str | None) -> list[dict[str, Any]]:
    """Volatile content only. The stable prefix lives in `system`.

    D-19's second breakpoint would sit between the pinned-lot block and the
    evidence block on a slow show, where a lot stays put for minutes. On a fast
    show it earns nothing and is not set — see the note in `_system_blocks`.
    """
    parts = [f"INTENT: {intent.value}", "", "FACTS:", _evidence_block(ev), "",
             f"MESSAGE FROM CHAT: {question}"]
    if repair:
        parts += ["", "YOUR PREVIOUS DRAFT WAS REJECTED:", repair,
                  "Rewrite it so every assertion is backed. Do not argue."]
    return [{"role": "user", "content": "\n".join(parts)}]


def _system_blocks(text: str) -> list[dict[str, Any]]:
    """One cached block. The prefix is ~900 tokens and frozen at import time:
    no timestamp, no uuid, no per-request data. Verified by
    `tests/test_llm.py::test_system_prefix_is_stable`, which is a test rather
    than a comment because a silent invalidator looks exactly like working code.
    """
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# =====================================================================
# Live client
# =====================================================================


class AnthropicClient:
    """The live path, with a breaker that falls back rather than erroring."""

    def __init__(self, *, record_to: Path | None = None,
                 breaker_threshold: int = 3) -> None:
        import anthropic

        self._c = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._record_to = record_to
        self._fails = 0
        self._threshold = breaker_threshold
        self.tripped = False
        self._fallback = ReplayClient(strict=False)

    # -- D-32 ------------------------------------------------------------

    def _degraded(self) -> bool:
        return self.tripped

    def _note_failure(self, exc: Exception) -> None:
        self._fails += 1
        # B-74. A 400 is the request being wrong, not the service being down, so
        # it must not be absorbed by a breaker designed for outages. Swapping
        # the drafting model to Haiku 4.5 for the Spike 1 ablation produced
        # "adaptive thinking is not supported on this model", three retries, and
        # then a SAFE REFUSAL — the arm scored 100% safe and 0% responsive, and
        # looked like a finding about the model rather than a typo in the call.
        # Degrading open is right for an outage and wrong for a misconfiguration.
        if getattr(exc, "status_code", None) == 400:
            log.error("llm call is MALFORMED, not failing: %s — this will not "
                      "be retried and the degraded reply is not a result", exc)
            self._fails = self._threshold
        log.warning("llm call failed (%d/%d): %s", self._fails, self._threshold, exc)
        if self._fails >= self._threshold:
            self.tripped = True
            log.error("circuit breaker tripped — serving fixtures. %s", exc)

    def _note_success(self) -> None:
        self._fails = 0

    # -- calls -----------------------------------------------------------

    def draft(self, *, question: str, evidence: Evidence, intent: Intent,
              repair: str | None = None,
              on_text: Callable[[str], None] | None = None) -> LLMResult:
        """Streamed, and thinking is set explicitly rather than left to default.

        **Streaming (B-14).** Not for throughput — for the metric. `DraftOutput`
        declares `reply_text` before `claims`, so the reply is complete on the
        wire while the claims are still decoding, and the operator can read it
        then. Measuring only the blocking total prices the operator's read time
        twice: once in the budget derivation and once in the wait.

        **Thinking (B-15).** The old call passed no `thinking` parameter and took
        the model default. It now says what it wants. The default turned out to
        be the right value and the wrong way to get it: an A/B found that turning
        thinking off saves 520 ms and raises over-blocking from 9.2% to 13.3%,
        so the setting is load-bearing and was never chosen. Passed from config
        so the comparison stays runnable rather than becoming a claim in a
        comment.
        """
        if self._degraded():
            return self._fallback.draft(question=question, evidence=evidence,
                                        intent=intent, repair=repair, on_text=on_text)
        msgs = _draft_messages(question, evidence, intent, repair)
        key = fixture_key(settings.draft_model, DRAFT_SYSTEM, msgs)
        t0 = time.perf_counter()
        ttft = 0
        try:
            with self._c.messages.stream(
                model=settings.draft_model,
                max_tokens=2048,          # 1024 truncated mid-JSON on long claim lists (B-12)
                system=_system_blocks(DRAFT_SYSTEM),
                messages=msgs,
                output_format=DraftOutput,
                thinking={"type": _thinking_for(settings.draft_model)},
            ) as stream:
                for chunk in stream.text_stream:
                    if not ttft:
                        ttft = int((time.perf_counter() - t0) * 1000)
                    if on_text is not None:
                        on_text(chunk)
                r = stream.get_final_message()
        except Exception as exc:
            self._note_failure(exc)
            return self._fallback.draft(question=question, evidence=evidence,
                                        intent=intent, repair=repair, on_text=on_text)
        self._note_success()
        res = _envelope(r, settings.draft_model, t0, key, ttft_ms=ttft)
        self._maybe_record(key, res)
        return res

    def classify(self, *, message: str) -> LLMResult:
        if self._degraded():
            return self._fallback.classify(message=message)
        msgs = [{"role": "user", "content": message}]
        key = fixture_key(settings.triage_model, TRIAGE_SYSTEM, msgs)
        t0 = time.perf_counter()
        try:
            r = self._c.messages.parse(
                model=settings.triage_model,
                max_tokens=256,           # a label and a float
                system=_system_blocks(TRIAGE_SYSTEM),
                messages=msgs,
                # Explicit, and for the opposite reason to the draft path.
                # D-17 always specified "no thinking" for triage; the code never
                # said so, so it inherited adaptive and the reasoning ate the
                # 256-token budget before the JSON was emitted — producing
                # `{"intent":"unkn` and a parse error, but only once the Suite A
                # threshold dropped far enough to escalate real volume.
                #
                # B-15 rejected disabling thinking on the DRAFT path because it
                # cost citation discipline. Nothing transfers: this call picks
                # one label from a closed set with worked examples in the prompt.
                # Same parameter, opposite correct value — which is exactly why
                # neither should be left to a default.
                thinking={"type": "disabled"},
                output_format=TriageOutput,
            )
        except Exception as exc:
            self._note_failure(exc)
            return self._fallback.classify(message=message)
        self._note_success()
        # An unparsable classification abstains rather than guessing: confidence
        # 0.0 routes to `unknown`, which surfaces the message without a draft
        # (D-13, D-14). Dropping it would be the one unsafe option — a silently
        # discarded buyer is the failure this whole stage exists to avoid.
        res = _envelope(r, settings.triage_model, t0, key,
                        on_unparsed=TriageOutput(
                            intent=Intent.UNKNOWN.value, referent="ambiguous",
                            seller_directed=True, confidence=0.0))
        self._maybe_record(key, res)
        return res

    def _maybe_record(self, key: str, res: LLMResult) -> None:
        if self._record_to is None:
            return
        self._record_to.mkdir(parents=True, exist_ok=True)
        payload = {"key": key, "model": res.model,
                   "output": res.output.model_dump(), "latency_ms": res.latency_ms}
        (self._record_to / f"{key}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _envelope(r: Any, model: str, t0: float, key: str, *, ttft_ms: int = 0,
              on_unparsed: Any = None) -> LLMResult:
    """B-12, and the second time it bit.

    `parsed_output` is None whenever generation was truncated or refused. That
    was fixed once, at the *draft* call site — so it came straight back on the
    classify path as an AttributeError on the first real eval run. A guard at one
    consumer is not a fix when the seam has two.

    `on_unparsed` is the caller's safe value for that case, so every path through
    this function returns a well-formed output and no consumer has to remember
    that None is possible.
    """
    u = r.usage
    parsed = r.parsed_output
    if parsed is None:
        log.warning("%s returned no parsable output (truncated or refused)", model)
        parsed = on_unparsed
    return LLMResult(
        output=parsed,
        model=model,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        ttft_ms=ttft_ms,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        fixture_key=key,
    )


# =====================================================================
# Replay — the no-key path and the failure mode, in one class
# =====================================================================


class ReplayClient:
    """Serves recorded fixtures. Deterministic, free, and needs no credential.

    `strict=True` raises on a missing fixture — what the golden-replay test
    wants, so a drifted prompt fails loudly. `strict=False` synthesises a safe
    refusal instead, which is what degradation wants: the reviewer gets a
    working console rather than a stack trace.
    """

    def __init__(self, fixture_dir: Path | None = None, *, strict: bool = True) -> None:
        self.dir = fixture_dir or FIXTURE_DIR
        self.strict = strict
        self.misses: list[str] = []

    def draft(self, *, question: str, evidence: Evidence, intent: Intent,
              repair: str | None = None,
              on_text: Callable[[str], None] | None = None) -> LLMResult:
        msgs = _draft_messages(question, evidence, intent, repair)
        key = fixture_key(settings.draft_model, DRAFT_SYSTEM, msgs)
        rec = self._load(key)
        if rec is None:
            return self._safe_draft(key, on_text)
        out = DraftOutput(**rec["output"])
        # One call with the finished text. A replayed draft did not stream, and
        # pretending otherwise by chunking it would fake a TTFT that was never
        # measured — `ttft_ms` stays 0, which is what "not streamed" means.
        if on_text is not None:
            on_text(out.reply_text)
        return LLMResult(output=out, model=rec["model"],
                         latency_ms=rec.get("latency_ms", 0), replayed=True,
                         fixture_key=key)

    def classify(self, *, message: str) -> LLMResult:
        msgs = [{"role": "user", "content": message}]
        key = fixture_key(settings.triage_model, TRIAGE_SYSTEM, msgs)
        rec = self._load(key)
        if rec is None:
            # Unknown routes to the operator without a draft, which is the
            # correct outcome for a message we cannot classify (D-13).
            return LLMResult(
                output=TriageOutput(intent=Intent.UNKNOWN.value, referent="ambiguous",
                                    seller_directed=False, confidence=0.0),
                model="replay", latency_ms=0, replayed=True, fixture_key=key)
        return LLMResult(output=TriageOutput(**rec["output"]), model=rec["model"],
                         latency_ms=rec.get("latency_ms", 0), replayed=True,
                         fixture_key=key)

    def _load(self, key: str) -> dict[str, Any] | None:
        p = self.dir / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        self.misses.append(key)
        if self.strict:
            raise FixtureMissing(key)
        return None

    def _safe_draft(self, key: str,
                    on_text: Callable[[str], None] | None = None) -> LLMResult:
        """Degraded mode still has to be safe. An empty claim list means every
        sentence is unsourced, so this text is deliberately free of numbers,
        superlatives and commitments — it passes the verifier on its merits
        rather than by exemption."""
        out = DraftOutput(reply_text="Let me check that and come back to you.",
                          claims=[], needs_clarification=False)
        if on_text is not None:
            on_text(out.reply_text)
        return LLMResult(output=out, model="replay-degraded", latency_ms=0,
                         replayed=True, fixture_key=key)


class FixtureMissing(KeyError):
    def __init__(self, key: str) -> None:
        super().__init__(f"no fixture for {key} — record one, or run with a live key")
        self.key = key


# =====================================================================
# Fixture keys
# =====================================================================


def fixture_key(model: str, system: str, messages: list[dict[str, Any]]) -> str:
    """Content hash of everything that determines the answer.

    Includes the system prompt deliberately: editing the claim contract must
    invalidate every fixture, because a fixture recorded under the old rules is
    no longer evidence of anything.
    """
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(system.encode())
    h.update(json.dumps(messages, sort_keys=True).encode())
    return h.hexdigest()[:20]


# =====================================================================
# Selection
# =====================================================================


# Adaptive thinking landed on Claude 4.6 and later; Haiku 4.5 rejects it with a
# 400 rather than ignoring it. Keeping the mapping here rather than in settings
# means a model swap on the command line cannot produce a silently degraded run.
_NO_ADAPTIVE = ("haiku-4-5", "haiku-3", "sonnet-3", "opus-3")


def _thinking_for(model: str) -> str:
    if any(m in model for m in _NO_ADAPTIVE):
        return "disabled"
    return settings.draft_thinking


def get_client(*, record: bool = False) -> LLMClient:
    """Live when a key is present, replay otherwise — no flag to forget.

    D-32: a reviewer with no credential gets a working system, not an error, and
    the same path is what a live outage degrades to.
    """
    if settings.use_live_llm:
        return AnthropicClient(record_to=FIXTURE_DIR if record else None)
    return ReplayClient(strict=False)
