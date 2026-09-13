"""Spike 2 — the properties Suite A cannot check.

Suite A measures *how well* the cascade scores against the incumbent. These are
the things that must hold regardless of the score, and that would still let the
aggregate look fine while the system was broken:

  1. a failing classifier surfaces the message rather than dropping it;
  2. the messages the incumbent provably misses stay caught (regression);
  3. every drop is explainable.

(1) is the one that matters. Dropping a buyer is silent — there is no error, no
log line the operator reads, and the aggregate barely moves. It is B-01's shape
again: a failure mode whose symptom is plausible output.
"""

from __future__ import annotations

import pytest

from app.llm import LLMResult, TriageOutput
from app.models import Intent
from app.triage import (
    Model,
    Route,
    TriageCascade,
    TriageResult,
    cluster,
    features,
    prefilter,
    rank,
    rule_intent,
)


class _Stub:
    """A classifier with a settable answer. Free, deterministic, and can fail."""

    def __init__(self, out=None, *, boom: bool = False, none_output: bool = False):
        self.out, self.boom, self.none_output = out, boom, none_output
        self.calls = 0

    def classify(self, *, message: str) -> LLMResult:
        self.calls += 1
        if self.boom:
            raise RuntimeError("classifier down")
        out = None if self.none_output else (self.out or TriageOutput(
            intent=Intent.AVAILABILITY_Q.value, referent="catalog",
            seller_directed=True, confidence=0.9))
        return LLMResult(output=out, model="stub", latency_ms=0)


@pytest.fixture(scope="module")
def model():
    return Model.load()


# --- stage 0 ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " ", "x"])
def test_prefilter_drops_single_characters(text):
    assert prefilter(text) is not None


@pytest.mark.parametrize("text", [
    "kartik joined", "someone won it for $340", "Giveaway entered"])
def test_prefilter_drops_platform_events(text):
    """Not user messages at all. They are signal for the engagement layer and
    noise for the reply queue — D-13's amendment after the first observation."""
    assert prefilter(text) is not None


def test_prefilter_keeps_a_real_question():
    assert prefilter("You got any psyducks") is None


# --- stage 1 ---------------------------------------------------------------


def test_entity_feature_fires_on_a_card_we_stock():
    """The load-bearing feature, on a card that is actually in the catalog."""
    f = features("Do u have the mew one")
    assert f["catalog_entity"] == 1.0
    assert f["question_mark"] == 0.0


def test_the_catalog_is_blind_to_what_we_do_not_stock():
    """B-18, pinned. `catalog_entity` cannot fire for "You got any psyducks"
    because Psyduck is not one of our fifteen lots — so the strongest feature is
    structurally blind on the question class that needs it most.

    The message is still caught, by `quantifier` + `second_person` + the loose
    operating point, not by knowing what a Psyduck is. Adding a species
    vocabulary to fix it properly was tried and rejected: cross-validation said
    it did not help and made the threshold less stable.
    """
    f = features("You got any psyducks")
    assert f["catalog_entity"] == 0.0
    assert f["quantifier"] == 1.0 and f["second_person"] == 1.0


def test_at_mention_is_detected_for_cross_user_suppression():
    """`cross_user` is 141 of 485 real messages and the biggest false-positive
    source — a viewer answering another viewer looks exactly like a question."""
    assert features("@flatsixgt3 check sold tab")["at_mention"] == 1.0


def test_request_verbs_fire_on_imperatives():
    for t in ("lugia next!", "Run the 2 left rayquaza", "Check comps"):
        assert features(t)["request_verb"] == 1.0, t


def test_score_is_a_probability(model):
    for t in ("wtf lmaooo", "You got any psyducks", "?" * 40):
        assert 0.0 <= model.score(features(t)) <= 1.0


def test_contributions_explain_the_score(model):
    """Every drop has to be answerable in one sentence. This is the entire
    reason the model is linear rather than an embedding similarity (D-15)."""
    f = features("@someone lol")
    contribs = model.contributions(f)
    assert contribs, "no feature fired; nothing to explain"
    assert all(isinstance(n, str) and isinstance(v, float) for n, v in contribs)
    # sorted by magnitude, strongest first
    mags = [abs(v) for _, v in contribs]
    assert mags == sorted(mags, reverse=True)


# --- the safety property ---------------------------------------------------


def test_classifier_failure_surfaces_rather_than_drops(model):
    """The one that matters.

    When the model is unavailable the cascade must fall *open*. A dropped buyer
    produces no error and barely moves any aggregate — it is invisible, which is
    exactly why it needs a test rather than a comment.
    """
    casc = TriageCascade(model=model, client=_Stub(boom=True))
    r = casc.triage("You got any psyducks")
    assert r.surfaced
    assert r.route is Route.SURFACED_CHEAP
    assert "classifier unavailable" in " ".join(r.reasons)
    assert r.intent is not Intent.UNKNOWN, "degraded still has to route"


def test_unparsable_classification_surfaces_rather_than_drops(model):
    """B-17. `parsed_output` is None on a truncated generation, and this stage
    must not be what breaks — the safe direction is to surface."""
    casc = TriageCascade(model=model, client=_Stub(none_output=True))
    assert casc.triage("You got any psyducks").surfaced


def test_low_confidence_does_not_veto(model):
    """A veto needs confidence. Below the model's own calibration floor the
    right route is `unknown` — surfaced without a draft (D-14)."""
    casc = TriageCascade(model=model, client=_Stub(TriageOutput(
        intent=Intent.HYPE_NOISE.value, referent="none",
        seller_directed=False, confidence=0.3)))
    r = casc.triage("You got any psyducks")
    assert r.surfaced
    assert r.intent is Intent.UNKNOWN


def test_confident_veto_drops(model):
    casc = TriageCascade(model=model, client=_Stub(TriageOutput(
        intent=Intent.CROSS_USER.value, referent="none",
        seller_directed=False, confidence=0.95)))
    r = casc.triage("You got any psyducks")
    assert not r.surfaced
    assert r.route is Route.DROPPED_BY_MODEL


def test_cheap_drop_costs_no_model_call(model):
    """The whole economic argument for a cascade. If obvious noise still called
    the model, stage 1 would be decoration."""
    stub = _Stub()
    casc = TriageCascade(model=model, client=stub)
    assert not casc.triage("wtf lmaoooo").surfaced
    assert stub.calls == 0


# --- regression: what the incumbent misses ---------------------------------

# Real messages from the held-out segment that Whatnot's question-mark
# highlighter drops and the gate catches. This is Spike 2's claim, pinned so a
# feature change cannot quietly give it back.
CAUGHT = [
    "320 for gare plz",
    "lugia next!",
    "Check comps",
    "Do u have the mew one",
    "Any team rocket holos",
    "Pre bid Lugia so I can sleep",
    "You got any psyducks",
    "did i miss the skyridge",
]

# Real messages the gate still misses at the shipped operating point. Pinned so
# the gap is a fact in the suite rather than a line in a report that goes stale
# — and so that a change which fixes one is visible immediately.
STILL_MISSED = ["no breaks!", "Go quicker"]


@pytest.mark.parametrize("text", CAUGHT)
def test_gate_catches_what_the_regex_misses(model, text):
    assert "?" not in text, "this test is only meaningful without a question mark"
    assert model.score(features(text)) >= model.threshold, text


@pytest.mark.parametrize("text", STILL_MISSED)
def test_known_gaps_stay_documented(model, text):
    """These fail today. The test asserts the gap so it cannot be forgotten; if
    one starts passing, this test fails and the list gets shorter."""
    assert model.score(features(text)) < model.threshold, (
        f"{text!r} now passes the gate — remove it from STILL_MISSED")


# --- stages 3 and 4 --------------------------------------------------------


def _r(text: str, intent: Intent = Intent.AVAILABILITY_Q, score: float = 0.9):
    return TriageResult(text=text, route=Route.SURFACED_MODEL, score=score,
                        intent=intent)


def test_cluster_collapses_duplicates_and_counts_them():
    """Eight people asking about psyducks is one card with a count of eight.
    The count is the demand signal, not bookkeeping."""
    out = cluster([_r("any psyducks"), _r("any psyducks"), _r("psyducks any"),
                   _r("what grade is the lugia")])
    assert len(out) == 2
    assert out[0].duplicates == 3
    assert out[1].duplicates == 1


def test_cluster_keeps_genuinely_different_questions():
    out = cluster([_r("any psyducks"), _r("what grade is the charizard")])
    assert len(out) == 2


def test_rank_puts_demand_above_a_lone_question():
    a, b = _r("any psyducks"), _r("any lugia")
    a.duplicates = 8
    assert rank([b, a])[0] is a


def test_rank_prefers_the_higher_value_intent():
    buy = _r("ill take lot 4", Intent.BUY_COMMIT)
    ship = _r("how much is shipping", Intent.SHIPPING_RETURNS_Q)
    assert rank([ship, buy])[0] is buy


# --- the degraded intent path ----------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("run the lugia", Intent.REQUEST),
    ("what psa grade is that", Intent.GRADE_CONDITION_Q),
    ("whats it worth", Intent.PRICE_VALUE_Q),
    ("do you ship to canada", Intent.SHIPPING_RETURNS_Q),
    ("is that real", Intent.AUTHENTICITY_Q),
])
def test_rule_intent_routes_without_a_model(text, want):
    """Crude on purpose. Its job is to route when the model cannot be called,
    and being wrong routes to a wider evidence set rather than a false answer."""
    assert rule_intent(text, features(text)) is want
