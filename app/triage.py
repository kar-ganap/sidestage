"""Spike 2 — the triage cascade. Which messages does the seller ever see?

The opponent is real and measured. Whatnot highlights chat messages in its own
UI, and `docs/research/chat-analysis-2026-09-12.md` establishes what that
highlighter is: across 477 observed messages **every** highlighted row contains
`?` and **no** unhighlighted row does. It is a question-mark regex, and it scores
**53.6% recall / 80.4% precision** pooled.

So the task is not "detect questions" — a regex already does that. It is two
opposite failures at once:

    intent without interrogative syntax     "lugia next!" · "320 for gare plz"
                                            "You got any psyducks"
    chatter that carries a question mark    "base set?" (asked of another viewer)
                                            "Yooo, atmosphere?? Song?"

Loosening a threshold fixes the first and worsens the second, which is why the
operating point has to be argued rather than picked (D-26).

THE CASCADE, cheapest first (D-15):

    0  prefilter    length, and platform events that are not user messages
    1  scorer       ~15 interpretable features -> one probability     ~6 ms
    2  escalation   the model classifies what survives                ~1 s
    3  cluster      eight people asking about psyducks is ONE card
    4  rank         what sits at the top of a two-second glance

Stage 1 measures ~6 ms/message, not the sub-millisecond D-15 predicted. Almost
all of it is the entity resolver, which is a fuzzy match over the catalog rather
than the trie lookup that estimate assumed. It is well inside the 50 ms budget,
so it is recorded rather than optimised — but the original figure was wrong and
the eval prints the real one.

Stage 1 is a **recall-oriented gate**, not the final answer. It exists to drop
the ~85% of traffic that is confidently noise without spending anything, and it
is tuned so that dropping is the conservative act. Stage 2 then classifies what
survives and may still veto it — which is what keeps precision from collapsing
when stage 1 is set loose.

WHY A LINEAR MODEL AND NOT EMBEDDINGS. Two reasons, and the second is the one
that matters. It has to run in under a millisecond at the head of a live stream;
and **every drop has to be explainable in one sentence**, because the failure
this system cannot afford is silently discarding a buyer. `reasons` carries the
features that actually moved the score. A cosine similarity cannot do that, and
it drags in a dependency that triples reviewer install (D-15).

The weights are FIT, not hand-tuned — `evals/fit_triage.py`, on the synthetic
train set plus the two real segments that are not the held-out test. Hand-tuning
would perform about as well and would not be defensible: "we chose 0.4" is not an
argument, and "fit on 676 labelled messages, reported on 161 held out" is.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.config import DATA_DIR
from app.entities import EntityResolver, get_resolver
from app.llm import LLMClient, get_client
from app.models import Intent

WEIGHTS_PATH = DATA_DIR / "triage_weights.json"


class Route(StrEnum):
    """What happened to a message, and why it is worth naming.

    `DROPPED_CHEAP` and `DROPPED_BY_MODEL` are both drops, but they cost
    different amounts and they fail differently — one is a scorer that was too
    aggressive, the other is a classifier that disagreed. Collapsing them would
    hide which stage to fix.
    """

    DROPPED_PREFILTER = "dropped_prefilter"
    DROPPED_CHEAP = "dropped_cheap"
    DROPPED_BY_MODEL = "dropped_by_model"
    SURFACED_CHEAP = "surfaced_cheap"
    SURFACED_MODEL = "surfaced_model"

    @property
    def surfaced(self) -> bool:
        return self in (Route.SURFACED_CHEAP, Route.SURFACED_MODEL)


# =====================================================================
# Stage 0 — filters that need no scoring
# =====================================================================

# Platform-generated rows. Never questions, never replied to, and genuinely
# signal for the engagement layer — but they must not enter the reply queue
# (D-13, amended after the first field observation).
_SYSTEM_PATTERNS = (
    re.compile(r"\b(joined|is here|welcome to the stream)\b", re.I),
    re.compile(r"\bwon\b.*\bfor\s*\$", re.I),
    re.compile(r"^\s*(unlocked|giveaway entered|order placed)\b", re.I),
)

MIN_CHARS = 2  # single-character spam was real in the observed chat


def prefilter(text: str) -> str | None:
    """Return a drop reason, or None to continue.

    Deliberately tiny. Anything with judgement in it belongs in the scorer where
    it gets a weight and shows up in `reasons`.
    """
    t = text.strip()
    if len(t) < MIN_CHARS:
        return "too short to carry intent"
    if any(p.search(t) for p in _SYSTEM_PATTERNS):
        return "platform event, not a user message"
    return None


# =====================================================================
# Stage 1 — features
# =====================================================================

_WH = re.compile(r"\b(what|which|who|whose|when|whens|where|why|how|hows)\b", re.I)
_AUX_INITIAL = re.compile(
    r"^\s*(is|are|was|were|do|does|did|can|could|would|will|should|have|has|had|any|got|u)\b",
    re.I)
# Imperatives that ask the seller to *act*. These are the messages the incumbent
# misses wholesale — "lugia next!", "Run the 2 left rayquaza", "Check comps".
_REQUEST_VERB = re.compile(
    r"\b(run|rerun|re-run|show|put\s*up|pull|flip|bring|check|post|list|send"
    r"|pre\s*bid|prebid|next|lmk|let\s*me\s*know)\b", re.I)
_SECOND_PERSON = re.compile(r"\b(you|u|ur|your|yours|yall|y'all)\b", re.I)
_QUANTIFIER = re.compile(r"\b(any|anymore|any\s*more|how\s*many|how\s*much|more)\b", re.I)
_POLITE = re.compile(r"\b(plz|pls|please|thx|thanks|ty)\b", re.I)
_AT_MENTION = re.compile(r"@\w+")
# An offer: a bare number attached to a quantity or a price preposition.
_PRICE_OFFER = re.compile(r"\b\d[\d,.]*\s*(k\b|for\b|all\b|each\b)|\bfor\s*\$?\d", re.I)
_NOISE = re.compile(
    r"\b(lmao+|lmfao+|lol+|rofl|wtf|omg|bruh|bro|goat|fire|insane|sheesh|damn"
    r"|clean|nice|wow|congrats|gz|gg|lets\s*go|letsgo|trollin|cap|no\s*cap)\b", re.I)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]", re.S)

FEATURE_NAMES = (
    "question_mark", "wh_word", "aux_initial", "second_person", "request_verb",
    "catalog_entity", "entity_live", "quantifier", "polite", "price_offer",
    "at_mention", "noise_lexicon", "emoji_only", "very_short", "long",
)


def features(text: str, resolver: EntityResolver | None = None) -> dict[str, float]:
    """Fifteen numbers, each of which a person can check by eye.

    Every one of them traces to something in the observed corpus rather than to
    intuition. `request_verb` exists because "no breaks!" and "lugia next!" are
    real messages the incumbent drops. `at_mention` exists because `cross_user`
    is 141 of 485 real messages and is the single biggest source of false
    positives — a viewer answering another viewer looks exactly like a question.
    """
    res = (resolver or get_resolver()).resolve(text)
    stripped = _EMOJI.sub("", text).strip()
    n_tokens = len(text.split())
    return {
        "question_mark": float("?" in text),
        "wh_word": float(bool(_WH.search(text))),
        "aux_initial": float(bool(_AUX_INITIAL.search(text))),
        "second_person": float(bool(_SECOND_PERSON.search(text))),
        "request_verb": float(bool(_REQUEST_VERB.search(text))),
        # The load-bearing one. Naming something currently for sale is high
        # intent regardless of phrasing — D-15's amendment after observation,
        # and the reason the entity trie had to exist for grounding anyway.
        "catalog_entity": float(bool(res.items or res.sets)),
        "entity_live": float(bool(res.lots)),
        "quantifier": float(bool(_QUANTIFIER.search(text))),
        "polite": float(bool(_POLITE.search(text))),
        "price_offer": float(bool(_PRICE_OFFER.search(text))),
        "at_mention": float(bool(_AT_MENTION.search(text))),
        "noise_lexicon": float(bool(_NOISE.search(text))),
        "emoji_only": float(not stripped),
        "very_short": float(n_tokens <= 2),
        "long": float(n_tokens >= 12),
    }


# =====================================================================
# Stage 1 — the scorer
# =====================================================================


@dataclass(frozen=True)
class Model:
    """A dot product and a bias. That is the entire model.

    Kept as plain floats rather than an array so scoring needs no numpy at
    runtime: the fitter imports numpy, the server does not. Fifteen
    multiplications is not a workload, and the install stays small (D-15).
    """

    weights: dict[str, float]
    bias: float
    threshold: float = 0.5
    """The operating point, and it ships WITH the weights on purpose.

    A threshold chosen by looking at a sweep over the test set is the best of
    nineteen tries, not an estimate — and holding a segment out buys nothing if
    the cutoff is picked after seeing it. So `evals/fit_triage.py` selects it on
    train and writes it here, and the eval reads it rather than choosing one.
    """
    fitted_on: str = ""
    n_train: int = 0

    @classmethod
    def load(cls, path=WEIGHTS_PATH) -> "Model":
        if not path.exists():
            # Unfitted is a real state — the repo must run before the fitter
            # has. A zero model surfaces everything, which is the safe
            # direction: over-surfacing wastes attention, under-surfacing
            # loses a buyer.
            return cls(weights=dict.fromkeys(FEATURE_NAMES, 0.0), bias=2.0)
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(weights=d["weights"], bias=d["bias"],
                   threshold=d.get("threshold", 0.5),
                   fitted_on=d.get("fitted_on", ""), n_train=d.get("n_train", 0))

    def score(self, f: dict[str, float]) -> float:
        z = self.bias + sum(self.weights.get(k, 0.0) * v for k, v in f.items())
        return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))

    def contributions(self, f: dict[str, float]) -> list[tuple[str, float]]:
        """What actually moved this score, largest magnitude first.

        This is the whole reason the model is linear. A drop the operator
        disagrees with has to be answerable with a sentence, not a shrug.
        """
        c = [(k, self.weights.get(k, 0.0) * v) for k, v in f.items() if v]
        return sorted(c, key=lambda kv: -abs(kv[1]))


# =====================================================================
# Result
# =====================================================================


@dataclass
class TriageResult:
    text: str
    route: Route
    score: float
    intent: Intent = Intent.UNKNOWN
    referent: str = "none"
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    feats: dict[str, float] = field(default_factory=dict)
    escalated: bool = False
    latency_ms: float = 0.0
    duplicates: int = 1

    @property
    def surfaced(self) -> bool:
        return self.route.surfaced


def _explain(model: Model, f: dict[str, float], limit: int = 3) -> list[str]:
    out = []
    for name, contrib in model.contributions(f)[:limit]:
        out.append(f"{'+' if contrib >= 0 else '-'}{abs(contrib):.2f} {name}")
    return out


# =====================================================================
# The cascade
# =====================================================================


class TriageCascade:
    """Stage 0-2. Clustering and ranking operate on batches and live below."""

    def __init__(self, *, model: Model | None = None,
                 resolver: EntityResolver | None = None,
                 client: LLMClient | None = None,
                 drop_below: float | None = None,
                 use_model: bool = True) -> None:
        self.model = model or Model.load()
        self.resolver = resolver or get_resolver()
        self._client = client
        # The fitted operating point, not the config default. `escalate_low`
        # predates the fit and is a hand-set band; the threshold that ships with
        # the weights was chosen on train data against the same features.
        self.drop_below = self.model.threshold if drop_below is None else drop_below
        self.use_model = use_model

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = get_client()
        return self._client

    def triage(self, text: str) -> TriageResult:
        t0 = time.perf_counter()

        # --- stage 0 ----------------------------------------------------
        reason = prefilter(text)
        if reason is not None:
            return TriageResult(text=text, route=Route.DROPPED_PREFILTER, score=0.0,
                                reasons=[reason],
                                latency_ms=(time.perf_counter() - t0) * 1000)

        # --- stage 1 ----------------------------------------------------
        f = features(text, self.resolver)
        s = self.model.score(f)
        why = _explain(self.model, f)

        if s < self.drop_below:
            return TriageResult(text=text, route=Route.DROPPED_CHEAP, score=s,
                                reasons=why, feats=f,
                                latency_ms=(time.perf_counter() - t0) * 1000)

        if not self.use_model:
            # The ablation arm: gate only, no model. Intent comes from the
            # rules below, which is exactly as good as rules get and is the
            # point of measuring it separately.
            return TriageResult(text=text, route=Route.SURFACED_CHEAP, score=s,
                                intent=rule_intent(text, f), reasons=why, feats=f,
                                latency_ms=(time.perf_counter() - t0) * 1000)

        # --- stage 2 ----------------------------------------------------
        # Everything that survives the gate is classified, because a surfaced
        # message needs an *intent* before evidence can be assembled — the
        # binary decision alone cannot route. The gate's job was to make sure
        # this call happens on ~15% of traffic rather than all of it.
        try:
            out = self.client.classify(message=text).output
        except Exception:
            # Degradation is a product surface, not an error (D-32). A message
            # that got this far is more likely than not worth a look, so an
            # unavailable classifier surfaces it with a rule-derived intent
            # rather than silently dropping a buyer.
            return TriageResult(text=text, route=Route.SURFACED_CHEAP, score=s,
                                intent=rule_intent(text, f), reasons=why + ["classifier unavailable"],
                                feats=f, latency_ms=(time.perf_counter() - t0) * 1000)

        # Belt as well as braces. `_envelope` now supplies an abstaining default
        # so `out` is never None (B-17), but this stage must not be the thing
        # that breaks if a client somewhere returns one anyway — dropping a
        # buyer on an AttributeError is the worst available outcome.
        if out is None:
            return TriageResult(text=text, route=Route.SURFACED_CHEAP, score=s,
                                intent=rule_intent(text, f), feats=f,
                                reasons=why + ["classifier returned nothing"],
                                latency_ms=(time.perf_counter() - t0) * 1000)
        try:
            intent = Intent(out.intent)
        except ValueError:
            intent = Intent.UNKNOWN

        ms = (time.perf_counter() - t0) * 1000
        # Stage 2 may veto. This is what holds precision up while the gate is
        # tuned for recall — but only when the model is *confident*, because
        # below its own calibration floor `unknown` is the right route (D-14).
        if not out.seller_directed and out.confidence >= 0.6:
            return TriageResult(text=text, route=Route.DROPPED_BY_MODEL, score=s,
                                intent=intent, referent=out.referent,
                                confidence=out.confidence, escalated=True,
                                reasons=why + [f"model: not seller-directed ({out.confidence:.2f})"],
                                feats=f, latency_ms=ms)

        return TriageResult(text=text, route=Route.SURFACED_MODEL, score=s,
                            intent=intent if out.confidence >= 0.6 else Intent.UNKNOWN,
                            referent=out.referent, confidence=out.confidence,
                            escalated=True, reasons=why, feats=f, latency_ms=ms)


# A coarse intent for the two paths that cannot call the model: the ablation arm
# and the degraded arm. Deliberately crude — its job is to route, and being
# wrong routes to a wider evidence set rather than to a false answer.
def rule_intent(text: str, f: dict[str, float]) -> Intent:
    if f.get("price_offer") and f.get("second_person"):
        return Intent.NEGOTIATION
    if f.get("request_verb"):
        return Intent.REQUEST
    # "any" plus either a card we know OR the seller being addressed. The second
    # half matters because of B-18: "You got any psyducks" names nothing the
    # catalog recognises, and requiring `catalog_entity` here would route the
    # commonest availability phrasing to `unknown` on exactly the degraded path
    # where routing is all we have.
    if f.get("quantifier") and (f.get("catalog_entity") or f.get("second_person")):
        return Intent.AVAILABILITY_Q
    if re.search(r"\b(psa|bgs|cgc|grade|graded|swirl|centering|cent|edge|whitening)\b",
                 text, re.I):
        return Intent.GRADE_CONDITION_Q
    if re.search(r"\b(price|worth|value|comp|comps|sell|sold|cost)\b", text, re.I):
        return Intent.PRICE_VALUE_Q
    if re.search(r"\b(ship|shipping|return|refund)\b", text, re.I):
        return Intent.SHIPPING_RETURNS_Q
    if re.search(r"\b(real|fake|authentic|legit|repro)\b", text, re.I):
        return Intent.AUTHENTICITY_Q
    if f.get("catalog_entity"):
        return Intent.ATTRIBUTE_Q
    return Intent.UNKNOWN


# =====================================================================
# Stage 3 — near-duplicate clustering
# =====================================================================

_DEDUPE_STRIP = re.compile(r"[^a-z0-9 ]+")


def _shingle(text: str) -> frozenset[str]:
    """Word set, with a crude plural stem.

    The stem is not decoration. The only genuine repeat in 161 real messages is
    `Any Blaziken?` / `Any Blazikens ?`, and on raw words those score 0.33
    Jaccard — {any, blaziken} against {any, blazikens} shares one token of
    three. The single feature clustering exists for was defeated by a trailing
    `s`, and the queue showed two cards where the operator sees one question
    asked twice.

    Trailing `s` only. Real stemming (Porter and friends) buys nothing here and
    would make the cluster key something nobody can predict by eye.
    """
    words = _DEDUPE_STRIP.sub(" ", text.lower()).split()
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def cluster(results: list[TriageResult], *, threshold: float = 0.6) -> list[TriageResult]:
    """Collapse near-duplicates, keeping a count.

    Eight people asking about psyducks is **one card with a count of eight**, not
    eight queue entries. The count is not bookkeeping — it is the demand signal,
    and it is what makes the queue ranking meaningful rather than chronological.

    Jaccard over word sets: crude, order-insensitive, and right for chat, where
    the same question arrives as "any psyducks", "psyducks?" and "you got
    psyduck". O(n^2) over a queue that is tens of items, not thousands.
    """
    kept: list[TriageResult] = []
    sets: list[frozenset[str]] = []
    for r in results:
        s = _shingle(r.text)
        if not s:
            kept.append(r)
            sets.append(s)
            continue
        for i, prev in enumerate(sets):
            if not prev:
                continue
            j = len(s & prev) / len(s | prev)
            if j >= threshold:
                kept[i].duplicates += 1
                break
        else:
            kept.append(r)
            sets.append(s)
    return kept


# =====================================================================
# Stage 4 — ranking
# =====================================================================

# What the seller should look at first when they glance for two seconds. A
# question about a lot that is live now is worth more than the same question
# about the shop, because the window to answer it closes (D-16).
_INTENT_VALUE: dict[Intent, float] = {
    Intent.BUY_COMMIT: 1.0,
    Intent.NEGOTIATION: 0.9,
    Intent.AVAILABILITY_Q: 0.8,
    Intent.PRICE_VALUE_Q: 0.75,
    Intent.REQUEST: 0.7,
    Intent.GRADE_CONDITION_Q: 0.7,
    Intent.ATTRIBUTE_Q: 0.65,
    Intent.AUTHENTICITY_Q: 0.6,
    Intent.SHIPPING_RETURNS_Q: 0.5,
    Intent.UNKNOWN: 0.4,
}


def rank(results: list[TriageResult], *, now_index: int | None = None) -> list[TriageResult]:
    """Order the queue: intent value x duplicate demand x recency.

    Recency is positional rather than wall-clock so the function stays pure and
    testable; the caller passes the index of the newest message.
    """
    n = now_index if now_index is not None else len(results)

    def key(item: tuple[int, TriageResult]) -> float:
        i, r = item
        recency = 1.0 / (1.0 + max(0, n - i) * 0.05)
        demand = 1.0 + math.log(r.duplicates) if r.duplicates > 1 else 1.0
        return _INTENT_VALUE.get(r.intent, 0.4) * demand * recency * (0.5 + r.score)

    return [r for _, r in sorted(enumerate(results), key=key, reverse=True)]


# =====================================================================


def triage_stream(texts: list[str], **kw) -> list[TriageResult]:
    """The whole cascade over a window of chat: score, drop, cluster, rank."""
    casc = TriageCascade(**kw)
    scored = [casc.triage(t) for t in texts]
    surfaced = [r for r in scored if r.surfaced]
    return rank(cluster(surfaced))
