"""Unit tests for app/verify.py — Spike 1's centrepiece.

WHY THIS FILE EXISTS, AND WHY THAT IS EMBARRASSING. Until now the verifier had
**zero** direct tests. `test_adapter.py` had 28 and `test_ledger.py` had 18, both
for code an adversarial review found unreachable from the running app. The one
component the entire safety claim rests on was covered only end-to-end, by a
suite that needs an API key, costs money and takes minutes — so in practice it
was covered by nothing that ran on every change.

An adversarial pass over the rewritten verifier then found **twelve** defects,
four of them fatal, every one reproducible in under a second with no model call.
The whole class was invisible because `pytest -q` was green and no test imported
`app.verify`.

So: one test per finding, named for the failure rather than the function, and
each carrying the repro in its docstring. A test whose reason for existing is
lost is a test that gets deleted the first time it is inconvenient.

Fixtures are hand-built, with the same value SHAPES the real assembler mints —
a test that loads `evals/data` breaks when the data moves for unrelated reasons,
and then gets muted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.catalog import get_catalog
from app.models import (
    Authority,
    Claim,
    ClaimType,
    Draft,
    Evidence,
    Fact,
    Verdict,
)
from app.verify import REGISTRY, verify

NOW = datetime(2026, 3, 1, tzinfo=UTC)

# Shapes copied from what `app.evidence.assemble` actually mints. Keeping them
# faithful is the point: a verifier reads `fact.value.get(...)`, so a test with
# a scalar where production has a dict tests nothing that can fail in the app.
SHAPE: dict[ClaimType, object] = {
    ClaimType.IDENTITY: {"name": "Charizard", "set": "Base Set",
                         "number": "4/102", "language": "en", "finish": "holo"},
    ClaimType.VARIANT: {"variants": ["shadowless"]},
    ClaimType.GRADE: {"grader": "PSA", "value": 9, "cert": "70223481",
                      "label": None, "raw": False, "condition": None},
    ClaimType.CENTERING: {"centering": 9.5, "edges": 9.0},
    ClaimType.CONDITION: {"observational": ["surface_scratches", "edge_wear"]},
    ClaimType.POP: {"as_of": "2026-02-20", "stale": False, "pop": 2967, "higher": 0},
    ClaimType.COMP: {"n": 5, "low": 305.0, "high": 370.0, "median": 330.0,
                     "window_days": 90, "quotable": True,
                     "phrase": "last 5 sold $305–$370, past 90d"},
    ClaimType.PRICE: {"price": 250.0, "operator_only": False},
    ClaimType.BID: {"current_bid": 890.0, "starting_bid": 250.0,
                    "reserve_met": False, "ends_at": None},
    ClaimType.AVAILABILITY: {"status": "live", "position": 6, "quantity": 3},
    ClaimType.SHIPPING: {"text": "Orders ship within 2 business days of payment "
                                 "clearing.", "clause": "shipping#1"},
    ClaimType.RETURNS: {"text": "Returns within 30 days.", "clause": "returns#1"},
    ClaimType.AUTHENTICITY: {"text": "Routed through authentication.",
                             "clause": "auth#1"},
    ClaimType.SIZING: {"size": "M"},        # never actually minted — see _sizing
}


def fact(fid: str, kind: ClaimType, value=None, *, authority=Authority.RECORD,
         note: str = "", subject: str = "lot_test", as_of=None,
         ttl_s: int | None = None) -> Fact:
    return Fact(id=fid, kind=kind, subject=subject,
                value=SHAPE[kind] if value is None else value,
                authority=authority, source="test", as_of=as_of or NOW,
                ttl_s=ttl_s, note=note or f"{kind.value} fact")


def ev(*facts: Fact, lot_id: str | None = None) -> Evidence:
    return Evidence(id="ev_test", lot_id=lot_id, facts=tuple(facts),
                    built_at=NOW, assembly_ms=0)


def draft(text: str, *claims: Claim) -> Draft:
    return Draft(id="d_test", card_id="c1", text=text, claims=list(claims),
                 evidence_id="ev_test", model="test", attempt=0, created_at=NOW)


def claim(kind: ClaimType, value: str, fid: str, quote: str) -> Claim:
    return Claim(type=kind, value=value, source_fact_id=fid, quote=quote)


def run(d: Draft, e: Evidence, **kw):
    return verify(d, e, catalog=get_catalog(), now=NOW, **kw)


def codes(result) -> set[str]:
    return {v.code for v in result.violations}


def blocked(d: Draft, e: Evidence, **kw) -> bool:
    return run(d, e, **kw).verdict is Verdict.BLOCKED


# =====================================================================
# Structural — a claim must actually point at something
# =====================================================================


def test_claim_citing_a_fact_that_does_not_exist_is_unrepairable():
    """No rewording makes a dangling citation valid, so a retry is wasted time
    (D-10b). The severity is the point, not the block."""
    e = ev(fact("f1", ClaimType.BID))
    d = draft("The bid is $890.", claim(ClaimType.BID, "890", "nope", "bid is $890"))
    r = run(d, e)
    assert "fact_not_found" in codes(r) and not r.repairable


def test_quote_absent_from_reply_is_caught():
    """A quote that is not in the reply covers nothing — the claim looks like it
    backs a sentence and silently does not."""
    e = ev(fact("f1", ClaimType.BID))
    d = draft("The bid is $890.", claim(ClaimType.BID, "890", "f1", "bid is $999"))
    assert "quote_not_in_reply" in codes(run(d, e))


def test_quote_spanning_two_sentences_is_caught():
    """B-09. One fact standing in for two assertions means only one is checked."""
    e = ev(fact("f1", ClaimType.BID))
    text = "The bid is $890. It ships tomorrow."
    d = draft(text, claim(ClaimType.BID, "890", "f1", text))
    assert "quote_spans_sentences" in codes(run(d, e))


# =====================================================================
# The registry must be total — B-37
# =====================================================================


def test_registry_covers_every_claim_type():
    """`REGISTRY.get(...)` returning None used to mean "skip". `identity` and
    `sizing` were both offered to the model and neither had an entry, so a claim
    of either type went wholly unchecked while still satisfying coverage."""
    assert not [t.value for t in ClaimType if t not in REGISTRY]


def test_unregistered_claim_type_fails_closed(monkeypatch):
    """And if one ever goes missing, the verifier blocks rather than skips."""
    import app.verify as V
    monkeypatch.delitem(V.REGISTRY, ClaimType.BID)
    e = ev(fact("f1", ClaimType.BID))
    d = draft("The bid is $890.", claim(ClaimType.BID, "890", "f1", "bid is $890"))
    r = run(d, e)
    assert "unverifiable_claim_type" in codes(r) and not r.repairable


@pytest.mark.parametrize("kind", sorted(REGISTRY, key=lambda k: k.value))
def test_every_verifier_rejects_a_fact_of_the_wrong_kind(kind: ClaimType):
    """B-33. Six of fourteen verifiers went straight to `fact.value.get(...)`,
    got None from a fact of the wrong kind, and returned a pass — B-04's
    mis-citation hole, on the mechanism this project leads with. Reproduced at
    the time: a `bid` claim of "$4" citing the *identity* fact, against a lot
    whose real bid was $890, verified clean."""
    wrong = ClaimType.BID if kind is not ClaimType.BID else ClaimType.IDENTITY
    e = ev(fact("f1", wrong))
    d = draft("Placeholder text here.", claim(kind, "x", "f1", "Placeholder"))
    assert blocked(d, e), f"{kind.value} accepted a {wrong.value} fact"


# =====================================================================
# Coverage — D-11, and the scope that makes it work in both directions
# =====================================================================


def test_uncited_number_blocks():
    """The backstop that makes the system fail closed."""
    e = ev(fact("f1", ClaimType.BID))
    assert "unbacked_claim" in codes(run(draft(
        "It has sold for 9999 dollars three times this week."), e))


def test_quoting_is_not_asserting():
    """B-37. Coverage keys on `claim.value`, not `claim.quote`: a one-word quote
    used to satisfy coverage for a whole sentence, so `quote='It'` laundered a
    fabricated 9999."""
    e = ev(fact("f1", ClaimType.BID))
    text = "It has sold for 9999 dollars three times this week."
    d = draft(text, claim(ClaimType.BID, "890", "f1", "It"))
    assert "unbacked_claim" in codes(run(d, e))


def test_a_comp_number_does_not_license_a_bid():
    """B-42, finding #2 — the flat exemption bag.

    `$305` sits inside the recorded comp range, so "the current bid is 305"
    passed against a real bid of $890 **with no bid claim at all**. Exemption is
    now per-sentence: the comp claim quotes sentence one and cannot vouch for
    sentence two.
    """
    e = ev(fact("f1", ClaimType.COMP), fact("f2", ClaimType.BID))
    text = ("Last 5 sold $305–$370, past 90d. "
            "The current bid on this one is 305.")
    d = draft(text, claim(ClaimType.COMP, "last 5 sold $305–$370, past 90d",
                          "f1", "Last 5 sold $305–$370, past 90d"))
    assert "unbacked_claim" in codes(run(d, e))


def test_the_buyer_cannot_choose_what_the_system_may_assert():
    """B-42 finding #3, and the reason `verify()` no longer takes a question.

    `_known_tokens` folded the buyer's question in verbatim, so asking "is it a
    psa 10?" exempted "10" and "Yes, this Charizard is a PSA 10" passed against
    a PSA 9 record. Same draft, empty question, blocked. **The buyer was
    choosing the verifier's exemption set** — a prompt-injection path in the
    safety component, added by a fix meant to stop over-blocking.

    Narrowing it was not enough (B-58): "Sorry, the price is already 999" and
    "Let me check with the host, we have 24 left" still passed, because
    apologies are not denials and deferring is not declining. So the question is
    no longer an input to verification at all — the signature does not accept
    one, which is the only version of this guarantee that cannot regress.
    """
    e = ev(fact("f1", ClaimType.IDENTITY))
    for t in ("Yes, this Charizard is a PSA 10.",
              "Yes, we will ship this Charizard today, guaranteed.",
              "Yes, this Charizard is gem mint.",
              "Sorry, the price is already 999 on that one."):
        assert blocked(draft(t, claim(ClaimType.IDENTITY, "Charizard", "f1", t)), e), t

    import inspect
    from app.verify import verify as _v
    assert "question" not in inspect.signature(_v).parameters, (
        "verify() must not accept a question: nothing outside the draft may "
        "widen what the draft is allowed to assert")


def test_a_number_the_reply_refuses_needs_no_fact():
    """B-37, and the reason dropping the question cost nothing.

    "We're at $890, so $320 wouldn't push it" repeats an offer in order to
    decline it. No fact contains $320 and none ever could, so blocking it left
    no repair available — the worst case the first adversarial pass found.

    The refusal is recognisable from the REPLY: the sentence denies the figure.
    That is what `_negated_near` reads, and a denial cannot be smuggled in from
    outside the draft the way a question could.
    """
    e = ev(fact("f1", ClaimType.BID))
    d = draft("We're at $890, so $320 wouldn't push it.",
              claim(ClaimType.BID, "890", "f1", "We're at $890"))
    assert not blocked(d, e)
    # ...but ASSERTING the same figure still needs a fact.
    t2 = "We're at $890, and $320 is where this one closes."
    assert blocked(draft(t2, claim(ClaimType.BID, "890", "f1", "We're at $890")), e)


@pytest.mark.parametrize("offered", ["320", "1,320", "1320"])
def test_a_refusal_works_above_four_figures(offered: str):
    """B-45. Spans kept their separators while the exemption set tokenised with
    `[a-z0-9]+`, so "1,320" could never match anything: the headline case worked
    only below four figures, as did every four-figure comp and pop in the
    catalog. Numbers are canonicalised on both sides now (`_numkey`)."""
    e = ev(fact("f1", ClaimType.BID))
    d = draft(f"We're at $890, so ${offered} wouldn't push it.",
              claim(ClaimType.BID, "890", "f1", "We're at $890"))
    assert not blocked(d, e)


def test_a_cited_policy_clause_is_not_an_unbacked_claim():
    """B-42, finding #4 — the same bug pointing the other way.

    The catalog's own shipping clause, quoted verbatim and correctly cited,
    BLOCKED, while "All sales are final" — the exact thing `_policy`'s docstring
    says must never be repeated — passed with no claims at all.
    """
    e = ev(fact("f1", ClaimType.SHIPPING,
                note="Orders ship within 2 business days of payment clearing."))
    text = "Orders ship within 2 business days of payment clearing."
    d = draft(text, claim(ClaimType.SHIPPING, "shipping#1", "f1", text))
    assert not blocked(d, e)
    assert blocked(draft("All sales are final and returns are not accepted."), e)


@pytest.mark.parametrize("text", [
    "Ships same day, tracked.",
    "Postage is on us worldwide, and we send it out the same afternoon.",
    "We have twenty of these left.",
    "We have a handful left.",
    "Ships same day and refunds are processed immediately.",
])
def test_uncited_promises_block_however_they_are_inflected(text: str):
    """B-43. `\\bship\\b` did not match "Ships" and `\\brefund\\b` did not match
    "refunds", and `_NUMBER` only ever saw digits — so a fabricated promise in
    ordinary English was never even examined. Spans and the exemption set are
    now compared on stems, so widening the patterns costs no false block."""
    assert blocked(draft(text), ev(fact("f1", ClaimType.AVAILABILITY)))


def test_a_promise_to_defer_is_not_a_promise_about_the_record():
    """B-53. The verifier's own messages prescribe deferral, and the recorded
    reply that follows that instruction blocked on `I'll` — B-24 again: a rule
    refusing the reply its own remedy asks for."""
    e = ev(fact("f1", ClaimType.CONDITION, authority=Authority.OBSERVATIONAL))
    d = draft("I'll have the host pull it up on camera so you can check surface.")
    assert not blocked(d, e)


def test_deferring_does_not_exempt_a_figure_or_a_superlative():
    """The bound on the rule above, asserted rather than hoped for."""
    e = ev(fact("f1", ClaimType.CONDITION, authority=Authority.OBSERVATIONAL))
    assert blocked(draft("I'll have the host ship it in 2 days."), e)
    assert blocked(draft("I'll have the host find you a perfect one."), e)


def test_ordinal_is_not_split_into_a_bare_number():
    """B-35. Without word boundaries `_NUMBER_RUN` turned "1st edition" into a
    bare "1" and "90d" into "90", blocking ordinary phrasing."""
    e = ev(fact("f1", ClaimType.VARIANT, {"variants": ["1st_edition"]},
                note="1st Edition on this copy"))
    d = draft("Yes, it's 1st Edition.",
              claim(ClaimType.VARIANT, "1st Edition", "f1", "it's 1st Edition"))
    assert "unbacked_claim" not in codes(run(d, e))


@pytest.mark.parametrize("text,value", [
    ("Current bid is $890.00 and the reserve hasn't been met yet.", "890.00"),
    ("It's a BGS 9.5 on centering.", "9.5"),
])
def test_a_decimal_point_does_not_end_a_sentence(text: str, value: str):
    """B-44. `[^.!?]+` split "$890.00" and "BGS 9.5" in two, firing
    `quote_spans_sentences` on one true sentence and then demanding a citation
    for the orphan fragment "Current bid is $890."."""
    kind = ClaimType.BID if "bid" in text else ClaimType.CENTERING
    e = ev(fact("f1", kind, note=f"recorded {value}"))
    d = draft(text, claim(kind, value, "f1", text))
    assert "quote_spans_sentences" not in codes(run(d, e))


def test_every_unbacked_sentence_gets_its_own_repair_instruction():
    """B-51. `_dedupe` keyed on `(code, claim_index)` and every coverage
    violation carries `claim_index=None`, so three independently unbacked
    sentences collapsed to one message about the first — the bounded retry fixed
    that sentence and blocked again on the next. A repair loop that can only
    ever make one pass of progress."""
    e = ev(fact("f1", ClaimType.GRADE))
    r = run(draft("It grades a gem mint. We will ship today. "
                  "There are 4000 in the world."), e)
    assert len([v for v in r.violations if v.code == "unbacked_claim"]) == 3


# =====================================================================
# Per-type rules
# =====================================================================


def test_negation_elsewhere_in_the_sentence_does_not_disarm_a_variant_block():
    """B-46, finding #1 — the flagship demo case, defeated by one word.

    `_variant` read `_is_negated(claim.quote)` over the WHOLE quote, so the most
    natural phrasing in the domain — affirming one variant while denying another
    — suppressed an UNREPAIRABLE block. `\\bno\\b` inside "no doubt" did it too.
    """
    e = ev(fact("f1", ClaimType.VARIANT,
                {"printed": ["reverse_holo"], "language": "en"},
                authority=Authority.CATALOG,
                note="Champion's Path (en) was printed as: reverse_holo"))
    for text in ("This copy is 1st Edition.",
                 "This copy is 1st Edition, not Shadowless.",
                 "It is 1st Edition, no doubt."):
        d = draft(text, claim(ClaimType.VARIANT, "1st Edition", "f1", text))
        assert "variant_not_printed" in codes(run(d, e)), text


def test_a_genuine_denial_of_an_unprinted_variant_still_passes():
    """The mirror. A rule that blocks the true case is not a safety rule."""
    e = ev(fact("f1", ClaimType.VARIANT,
                {"printed": ["reverse_holo"], "language": "en"},
                authority=Authority.CATALOG,
                note="Champion's Path (en) was printed as: reverse_holo"))
    text = "No, this copy is not 1st Edition."
    d = draft(text, claim(ClaimType.VARIANT, "1st Edition", "f1", text))
    assert "variant_not_printed" not in codes(run(d, e))


def test_identity_claim_cannot_launder_a_fabricated_grade():
    """B-37. `identity` was the second most common claim type in the recorded
    fixtures and had no verifier at all."""
    e = ev(fact("f1", ClaimType.IDENTITY))
    text = "It's the Base Set Charizard 4/102, a PSA 10."
    d = draft(text, claim(ClaimType.IDENTITY,
                          "Base Set Charizard 4/102 PSA 10", "f1", text))
    assert blocked(d, e)


def test_identity_claim_matching_the_record_passes():
    e = ev(fact("f1", ClaimType.IDENTITY))
    text = "It's the Base Set Charizard 4/102."
    d = draft(text, claim(ClaimType.IDENTITY, "Base Set Charizard 4/102",
                          "f1", "the Base Set Charizard 4/102"))
    assert run(d, e).verdict is Verdict.PASS


def test_the_ambiguity_clarifier_is_not_an_identity_mismatch():
    """B-48, finding #7. When the reference is ambiguous `assemble` mints an
    identity fact holding the CANDIDATES — D-14's whole mechanism — and token
    containment rejected the clarifier for naming them properly, UNREPAIRABLY,
    so it went straight to the fallback. Asking is not asserting."""
    e = ev(fact("f1", ClaimType.IDENTITY,
                {"ambiguous": True, "question": "which one? — Mew or Mew ex",
                 "candidates": [{"name": "Mew", "set": "Celebrations",
                                 "number": "011/025", "grade": "RAW"}]},
                note="AMBIGUOUS — ask, do not answer: which one?"))
    text = "Which Mew do you mean, the Celebrations 011/025 one?"
    d = draft(text, claim(ClaimType.IDENTITY, "Mew", "f1", text))
    assert "identity_mismatch" not in codes(run(d, e))


def test_centering_measurement_must_be_one_the_record_contains():
    """B-47, finding #8. `_centering` never looked at `claim.value` — the
    condition after the kind guard was dead, so a fabricated "60/40" against a
    recorded 9.5 subgrade passed and was ledgered as checked."""
    e = ev(fact("f1", ClaimType.CENTERING, note="BGS subgrades: centering 9.5"))
    text = "Centering is 60/40 left to right."
    d = draft(text, claim(ClaimType.CENTERING, "60/40", "f1", text))
    assert blocked(d, e)


@pytest.mark.parametrize("value", ["3 bids in, currently at $890",
                                   "currently at $890, 3 bids in"])
def test_word_order_inside_a_claim_does_not_decide_the_verdict(value: str):
    """B-49, finding #10. Every numeric verifier read `_numbers(...)[0]`, so
    "3 bids in, currently at $890" blocked as a $3 bid while the same sentence
    reordered passed. Identical facts, identical truth, opposite verdicts."""
    e = ev(fact("f1", ClaimType.BID))
    text = "3 bids in, currently at $890."
    d = draft(text, claim(ClaimType.BID, value, "f1", text))
    assert "bid_mismatch" not in codes(run(d, e))


def test_a_false_bid_still_blocks():
    """The bound on the rule above: presence, not position — a claim naming only
    a false figure still has no match."""
    e = ev(fact("f1", ClaimType.BID))
    text = "Currently at $305."
    d = draft(text, claim(ClaimType.BID, "305", "f1", text))
    assert "bid_mismatch" in codes(run(d, e))


def test_comp_sample_size_and_window_are_not_read_as_prices():
    """B-52, finding #11. `_numbers(claim.quote)` picked up the 5 and the 90 from
    "last 5 sold ... past 90 days", so `min(nums)` fired `comp_outside_range` on
    any rewording of the sanctioned phrase."""
    e = ev(fact("f1", ClaimType.COMP))
    text = "Over the last 5 sales in the past 90 days these ran $305 to $370."
    d = draft(text, claim(ClaimType.COMP, "$305 to $370", "f1", text))
    assert "comp_outside_range" not in codes(run(d, e))


def test_observational_attribute_cannot_be_asserted():
    """D-12. What the seller can see on camera is not a record. On raw cards
    this is the common case, not an edge case."""
    e = ev(fact("f1", ClaimType.CENTERING, {"centering": "looks good"},
                authority=Authority.OBSERVATIONAL))
    text = "The centering is 55/45."
    d = draft(text, claim(ClaimType.CENTERING, "55/45", "f1", text))
    assert "centering_no_subgrade" in codes(run(d, e))


def test_sizing_is_unrepairable_because_no_sizing_fact_is_ever_minted():
    """B-50. It returned a REPAIRABLE mis-citation, so every sizing question
    burned the ~2.3 s bounded retry by construction before blocking anyway —
    against D-10b's own argument."""
    e = ev(fact("f1", ClaimType.IDENTITY))
    d = draft("It's a medium.", claim(ClaimType.SIZING, "M", "f1", "a medium"))
    r = run(d, e)
    assert r.verdict is Verdict.BLOCKED and not r.repairable


# =====================================================================
# Cost — the property the whole design exists for
# =====================================================================


def test_verification_is_cheap_because_evidence_is_already_in_hand():
    """The architectural claim in one assertion: verification is dict lookups
    over evidence assembled *before* generation, so it costs microseconds and
    can sit on the send path. If it ever regresses to a network call, the
    latency budget in DECISIONS.md stops being true."""
    import time
    e = ev(fact("f1", ClaimType.BID, note="current bid 890"))
    d = draft("The bid is at 890 right now.",
              claim(ClaimType.BID, "890", "f1", "bid is at 890"))
    t = time.perf_counter()
    for _ in range(100):
        run(d, e)
    ms = (time.perf_counter() - t) * 1000 / 100
    assert ms < 5.0, f"verification took {ms:.2f}ms/call"


def test_empty_draft_is_not_a_pass_by_the_pipeline():
    """The verifier alone passes an empty reply — every rule it has asks "is
    this assertion supported", and there are no assertions. That is correct and
    incomplete, which is why `pipeline.draft_reply` adds the emptiness check.
    Pinning it here so neither side silently drops it."""
    assert run(draft(""), ev(fact("f1", ClaimType.BID))).verdict is Verdict.PASS


# =====================================================================
# The passes nothing was testing
#
# An adversarial pass mutation-tested this file: it deleted a rule, ran the
# suite, and recorded whether anything went red. **Eleven distinct mutants
# survived all 53 tests** — including deleting the reserve-leak pass outright,
# deleting `_grade` entirely, and turning `_numkey` into the identity function.
# A green suite that survives the deletion of the rule it is meant to protect is
# measuring nothing. Each test below kills one of those mutants.
# =====================================================================


def test_the_reserve_never_reaches_the_room():
    """`operator_only_off` survived every test. The reserve leak is the most
    damaging thing this system can do to its own user — it destroys their
    negotiating position — and nothing asserted that it blocks."""
    e = ev(fact("f1", ClaimType.PRICE, {"reserve": 1100.0, "operator_only": True},
                note="reserve $1,100.00 — OPERATOR ONLY"))
    for text in ("I can go down to $1,100 on this one.",
                 "The reserve is 1100."):
        r = run(draft(text), e)
        assert "operator_only_leaked" in codes(r), text
        assert not r.repairable


def test_a_reserve_inside_a_quoted_comp_range_is_not_a_leak():
    """B-10, the bound on the rule above. lot_006's reserve is $1,100 while its
    comps run $1,150–$1,310; a quoted range that straddles the reserve is not a
    leak, and flagging it blocked legitimate comp answers."""
    e = ev(fact("f1", ClaimType.PRICE, {"reserve": 1100.0, "operator_only": True}),
           fact("f2", ClaimType.COMP, {"n": 5, "low": 1100.0, "high": 1310.0,
                                       "quotable": True, "window_days": 90,
                                       "phrase": "last 5 sold $1,100–$1,310"}))
    text = "last 5 sold $1,100–$1,310"
    d = draft(text, claim(ClaimType.COMP, text, "f2", text))
    assert "operator_only_leaked" not in codes(run(d, e))


@pytest.mark.parametrize("value,fv,code", [
    ("9", {"grader": "RAW", "value": None, "cert": None, "raw": True},
     "grade_on_raw_card"),
    ("10", {"grader": "PSA", "value": 9, "cert": "70223481", "raw": False},
     "grade_mismatch"),
    ("9", {"grader": "PSA", "value": 9, "cert": None, "raw": False},
     "grade_cert_missing"),
])
def test_grade_rules(value: str, fv: dict, code: str):
    """`grade_off` — deleting everything in `_grade` except the kind guard —
    survived all 53 tests."""
    e = ev(fact("f1", ClaimType.GRADE, fv))
    text = f"It's a PSA {value}."
    assert code in codes(run(draft(text, claim(ClaimType.GRADE, value, "f1", text)), e))


def test_a_cert_number_is_not_a_claimed_grade():
    """B-10. An eight-digit cert sits in the same sentence as the grade, and a
    naive read treated cert 70223481 as a claimed grade of 70223481. Found by
    the B2 control set on a benign question."""
    e = ev(fact("f1", ClaimType.GRADE))
    text = "It's a PSA 9, cert 70223481."
    d = draft(text, claim(ClaimType.GRADE, "PSA 9 cert 70223481", "f1", text))
    assert "grade_mismatch" not in codes(run(d, e))


def test_denying_a_grade_on_a_raw_card_is_the_correct_answer():
    """B-24. The record establishes the absence, so a claim asserting the
    absence is supported by it — they are the same statement."""
    e = ev(fact("f1", ClaimType.GRADE,
                {"grader": "RAW", "value": None, "cert": None, "raw": True}))
    text = "It's raw and listed as NM, so I can't call it a PSA 9."
    d = draft(text, claim(ClaimType.GRADE, "not graded, raw", "f1",
                          "It's raw and listed as NM"))
    assert "grade_on_raw_card" not in codes(run(d, e))


def test_policy_must_come_from_a_policy_fact_not_from_what_sounds_standard():
    """`policy_kindonly` survived. Observed live: a seller announced "all
    auction sales are final" on stream, which may conflict with platform buyer
    protection — repeating it would manufacture liability for our own user."""
    e = ev(fact("f1", ClaimType.RETURNS, {"text": "Returns are handled case by case."},
                note="no clause id"))
    text = "All sales are final."
    r = run(draft(text, claim(ClaimType.RETURNS, "sales are final", "f1", text)), e)
    assert "policy_uncited" in codes(r) and not r.repairable


def test_a_value_gated_clause_does_not_apply_below_its_gate():
    """The other half of `policy_kindonly`. eBay routes items at or above $250
    through authentication; lot_s02 is a $24 shop listing, so promising it
    authentication is a promise the platform will not keep."""
    text = "This one goes through authentication."
    d = draft(text, claim(ClaimType.AUTHENTICITY, "auth#1", "f1",
                          "goes through authentication"))
    e = ev(fact("f1", ClaimType.AUTHENTICITY,
                {"text": "Routed through authentication.", "clause": "auth#1",
                 "min_item_value": 250.0}), lot_id="lot_s02")
    r = run(d, e)
    assert "authenticity_value_gate" in codes(r) and not r.repairable
    # ...and on a lot above the gate the same reply is fine.
    e2 = ev(fact("f1", ClaimType.AUTHENTICITY,
                 {"text": "Routed through authentication.", "clause": "auth#1",
                  "min_item_value": 250.0}), lot_id="lot_006")
    assert "authenticity_value_gate" not in codes(run(d, e2))


@pytest.mark.parametrize("text,value,code", [
    ("We've got plenty of these left.", "plenty", "unbounded_quantifier"),
    ("There are 9 left.", "9", "availability_mismatch"),
])
def test_availability_rules(text: str, value: str, code: str):
    """`availability_kindonly` survived all 53 tests."""
    e = ev(fact("f1", ClaimType.AVAILABILITY,
                {"status": "live", "position": 6, "quantity": 3}))
    assert code in codes(run(draft(
        text, claim(ClaimType.AVAILABILITY, value, "f1", text)), e))


@pytest.mark.parametrize("value,code", [
    ("$305", "bare_comp"),
    ("$305 to $900", "comp_outside_range"),
])
def test_comp_rules(value: str, code: str):
    """`comp_phrase_only` survived. Primer §5: a comp is never a bare number."""
    e = ev(fact("f1", ClaimType.COMP))
    text = f"These run {value}."
    assert code in codes(run(draft(
        text, claim(ClaimType.COMP, value, "f1", text)), e))


def test_a_comp_we_cannot_quote_must_be_declined_not_estimated():
    e = ev(fact("f1", ClaimType.COMP,
                {"n": 1, "quotable": False, "reason": "only 1 sale in 90d"}))
    text = "These go for about $400."
    r = run(draft(text, claim(ClaimType.COMP, "$400", "f1", text)), e)
    assert "comp_not_quotable" in codes(r) and not r.repairable


@pytest.mark.parametrize("text,code", [
    ("Honestly it's guaranteed to go up, safe investment.", "investment_advice"),
    ("This one is 100% real, no question.", "authenticity_overclaim"),
    ("The surface is flawless on this copy.", "observational_assertion"),
])
def test_a_banned_phrasing_fires_whatever_else_is_cited(text: str, code: str):
    """`lexical_off` — deleting the whole `policies.json` pass — survived the
    first version of this test, because coverage happened to fire on the same
    sentence and the assertion was only "something blocked". Naming the CODE is
    what makes it a test of this rule rather than of any rule.

    These fire even on a well-cited claim: some phrasings are wrong however well
    evidenced the underlying fact is, and `investment_advice` is the one that is
    a regulated statement rather than a factual error."""
    e = ev(fact("f1", ClaimType.COMP))
    assert code in codes(run(draft(text), e))


def test_investment_advice_is_never_repairable():
    """The severity is the product decision: no rewording makes a solo seller
    licensed to give it."""
    e = ev(fact("f1", ClaimType.COMP))
    r = run(draft("Honestly it's guaranteed to go up, safe investment."), e)
    assert not r.repairable


def test_a_card_name_is_not_a_shipping_promise():
    """`proper_nouns_off` survived. `itm_swshp_special_delivery_pikachu` is a
    real card in the shipped catalog and `_COMMITMENT` matches "Delivery", so
    naming the card demanded a citation no fact could ever satisfy."""
    e = ev(fact("f1", ClaimType.IDENTITY,
                {"name": "Special Delivery Pikachu", "set": "SWSH Black Star Promos",
                 "number": "074", "language": "en", "finish": "holo"}))
    # Deliberately NO claim: with one, the cited fact would exempt the word
    # anyway and the test would not isolate this rule. A name is exempt because
    # it is a name, wherever in the reply it appears.
    assert not blocked(draft("This is the Special Delivery Pikachu."), e)


def test_a_name_exempts_the_word_but_never_a_number():
    """The bound: a title is a name, not a licence to state figures. The
    docstring said so before the code did — `_WORD` is `[a-z0-9]+`, so every
    DIGIT in an identity fact was silently exempt too, and a lot listed at
    $24.00 made "we have 24 left" assertable with no claim at all."""
    e = ev(fact("f1", ClaimType.IDENTITY,
                {"name": "Charizard", "set": "Base Set", "number": "4/102",
                 "language": "en", "finish": "holo"}),
           fact("f2", ClaimType.PRICE, {"price": 24.0, "operator_only": False},
                note="$24.00"))
    assert blocked(draft("We have 24 of these left."), e)


def test_a_fact_that_moved_mid_request_blocks():
    """`staleness_off` — deleting D-09 entirely — survived. An evidence
    snapshot goes stale INSIDE the request: ~800ms passes between assembly and
    verification and on an auction lot a bid lands in that window."""
    from datetime import timedelta
    cat = get_catalog()
    lot = next(l for l in cat.lots.values() if l.current_bid)
    moved = fact("f1", ClaimType.BID,
                 {"current_bid": lot.current_bid - 50, "starting_bid": 1.0,
                  "reserve_met": False, "ends_at": None},
                 subject=lot.id, as_of=NOW - timedelta(seconds=60), ttl_s=1)
    r = verify(draft("ok"), ev(moved, lot_id=lot.id), catalog=cat, now=NOW)
    assert "stale_evidence" in codes(r)
    # A fact still inside its TTL must NOT fire, or every request blocks.
    fresh = fact("f1", ClaimType.BID,
                 {"current_bid": lot.current_bid - 50, "starting_bid": 1.0,
                  "reserve_met": False, "ends_at": None},
                 subject=lot.id, as_of=NOW, ttl_s=30)
    assert "stale_evidence" not in codes(
        verify(draft("ok"), ev(fresh, lot_id=lot.id), catalog=cat, now=NOW))


# ---- the helpers the whole coverage rewrite rests on ----------------------


def test_a_number_is_compared_as_a_number_not_as_a_string():
    """`numkey_id` survived all 53 tests, and this is the single highest-yield
    rule in the file: every figure `assemble` mints is a float and every note
    renders money as `$890.00`, while the model writes `$890`. String comparison
    made the verdict on a true, correctly-cited sentence depend on whether the
    model happened to type the cents — it blocked half the recorded corpus."""
    e = ev(fact("f1", ClaimType.BID, {"current_bid": 890.0, "starting_bid": 250.0,
                                      "reserve_met": False, "ends_at": None},
                note="auction, current bid $890.00"))
    text = "Current bid is $890 right now."
    d = draft(text, claim(ClaimType.BID, "$890.00", "f1", text))
    assert not blocked(d, e), "the fact holding 890.0 must exempt the reply's '890'"


def test_a_word_is_compared_by_lemma_not_by_suffix_stripping():
    """`stem_id` survived. The stemmer this replaced was wrong in both
    directions: it collided "lots" with "lot" — and `assemble` mints a
    "lot is <status>" note for every lot, so the commonest unbounded quantifier
    exempted itself — while leaving "guarantee"/"guaranteed" unequal."""
    e = ev(fact("f1", ClaimType.SHIPPING,
                note="Orders ship within 2 business days of payment clearing."))
    text = "Ships within 2 business days."
    d = draft(text, claim(ClaimType.SHIPPING, "shipping#1", "f1", text))
    assert not blocked(d, e), "'Ships' must match a fact that says 'ship'"


def test_lots_of_does_not_exempt_itself_against_the_word_lot():
    """The mirror, and the reason the stemmer had to go."""
    e = ev(fact("f1", ClaimType.AVAILABILITY,
                {"status": "live", "position": 6, "quantity": 0},
                note="lot is live"))
    assert blocked(draft("We have lots of these in the shop."), e)


def test_a_claim_only_speaks_for_the_sentence_its_quote_sits_in():
    """The scoping rule the whole rewrite turns on. A comp claim quoting
    sentence one must not lend its fact's figures to sentence two."""
    e = ev(fact("f1", ClaimType.COMP), fact("f2", ClaimType.BID))
    text = "Last 5 sold $305–$370, past 90d. The bid is at 370."
    d = draft(text, claim(ClaimType.COMP, "last 5 sold $305–$370, past 90d",
                          "f1", "Last 5 sold $305–$370, past 90d"))
    assert "unbacked_claim" in codes(run(d, e))


def test_an_ambiguous_reference_must_be_asked_about_not_answered():
    """B-63. `amb_off` survived, and the test that named this fix was vacuous:
    its claim value was "Mew", which the pre-fix token containment also
    accepted, and it asserted only the absence of one code rather than a pass.
    Both halves are asserted here."""
    amb = fact("f1", ClaimType.IDENTITY,
               {"ambiguous": True, "question": "which one? — Mew or Mew ex",
                "candidates": [{"name": "Mew", "set": "Celebrations",
                                "number": "011/025", "grade": "RAW"},
                               {"name": "Mew ex", "set": "EX FireRed & LeafGreen",
                                "number": "107/112", "grade": "PSA 9"}]},
               note="AMBIGUOUS — ask, do not answer: which one?")
    e = ev(amb)
    ask = "Which Mew do you mean, the Celebrations 011/025 or the EX FireRed & LeafGreen PSA 9?"
    assert not blocked(draft(ask, claim(ClaimType.IDENTITY,
                                        "Celebrations 011/025", "f1", ask)), e)
    # ...and answering about one of them, using the OTHER's grade, must not pass.
    tell = "That Celebrations Mew 011/025 is a PSA 9 and it is authentic."
    assert blocked(draft(tell, claim(ClaimType.IDENTITY,
                                     "Celebrations Mew 011/025 PSA 9", "f1", tell)), e)
