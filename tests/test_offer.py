"""The buyer's own figure — every direction, because nothing tested any of them.

WHY THIS FILE EXISTS (B-116). `_offer` and `offer_asserted_as_price` are the
third answer to B-37 and the most safety-critical thing added in two waves. An
adversarial pass grepped `tests/` for `offer_asserted_as_price`,
`offer_misquoted`, `_INTERJECTION`, `_soft`, `Spread` and `_require_key` and
found **no file mentioning any of them**. It then wrote 19 mutants the project's
own harness does not define and **15 survived** — including deleting the entire
`_price` offer branch, which flips the flagship repro from blocked to pass.

`tools/mutate.py`'s headline of "32/32 killed" was true of the 32 mutants it
happens to define. That is the exact sentence its own docstring uses to condemn
the harness before it. The lesson is not "write a better mutant list" — it is
that a test written by whoever wrote the fix covers the cases they thought of,
so the tests have to be written from the ATTACK, not from the intent.

Every test below is one of that reviewer's surviving mutants, turned into an
assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.catalog import get_catalog
from app.entities import get_resolver
from app.evidence import assemble
from app.models import Authority, Claim, ClaimType, Draft, Evidence, Fact, Intent, Verdict
from app.verify import verify

NOW = datetime(2026, 3, 1, tzinfo=UTC)
CAT = get_catalog()


def offer_fact(*said: float) -> Fact:
    return Fact(id="fo", kind=ClaimType.PRICE, subject="buyer",
                value={"buyer_said": list(said), "operator_only": False,
                       "is_offer": True},
                authority=Authority.OBSERVATIONAL, source="chat.message",
                as_of=NOW,
                note="the buyer named " + ", ".join(f"${v:,.2f}" for v in said))


def bid_fact() -> Fact:
    return Fact(id="fb", kind=ClaimType.BID, subject="lot_006",
                value={"current_bid": 890.0, "starting_bid": 250.0,
                       "reserve_met": False, "ends_at": None},
                authority=Authority.RECORD, source="t", as_of=NOW,
                note="auction, current bid $890.00")


def run(text: str, *claims: Claim, facts: tuple[Fact, ...]):
    ev = Evidence(id="e", lot_id=None, facts=facts, built_at=NOW, assembly_ms=0)
    d = Draft(id="d", card_id="c", text=text, claims=list(claims),
              evidence_id="e", model="t", attempt=0, created_at=NOW)
    return verify(d, ev, catalog=CAT, now=NOW)


def codes(r) -> set[str]:
    return {v.code for v in r.violations}


def blocked(r) -> bool:
    return r.verdict is Verdict.BLOCKED


# =====================================================================
# The direction nothing asserted: a buyer figure ASSERTED must block
# =====================================================================


def test_asserting_the_buyers_figure_as_a_price_blocks():
    """The flagship repro, and `offer__price_branch_deleted` — the mutant that
    removes the whole fix — flips it to pass with the suite green."""
    r = run("Yes — these go for $6,200.00.",
            Claim(type=ClaimType.PRICE, value="$6,200.00", source_fact_id="fo",
                  quote="these go for $6,200.00"),
            facts=(offer_fact(6200.0),))
    assert "offer_asserted_as_price" in codes(r) and not r.repairable


def test_declining_the_buyers_figure_passes():
    """The mirror. A rule that blocks the correct refusal is B-37 all over."""
    r = run("We're at $890, so $320 wouldn't push it.",
            Claim(type=ClaimType.BID, value="890", source_fact_id="fb",
                  quote="We're at $890"),
            Claim(type=ClaimType.PRICE, value="320", source_fact_id="fo",
                  quote="$320 wouldn't push it"),
            facts=(bid_fact(), offer_fact(320.0)))
    assert not blocked(r), codes(r)


def test_a_figure_the_buyer_never_said_cannot_be_cited_as_an_offer():
    """`offer__misquote_check_deleted` survived."""
    r = run("I can't do $4,100.",
            Claim(type=ClaimType.PRICE, value="4100", source_fact_id="fo",
                  quote="I can't do $4,100"),
            facts=(offer_fact(320.0),))
    assert "offer_misquoted" in codes(r)


def test_the_refusal_is_unrepairable_not_a_rewrite_away():
    """`offer__unrepairable_downgraded` survived. No rewording makes the buyer's
    number a fact about the card, so a repair attempt is 2.3s of wasted budget
    (D-10b)."""
    r = run("These go for $6,200.00.",
            Claim(type=ClaimType.PRICE, value="$6,200.00", source_fact_id="fo",
                  quote="These go for $6,200.00"),
            facts=(offer_fact(6200.0),))
    assert not r.repairable


# =====================================================================
# The hole the reviewer found: one decline licensing every other figure
# =====================================================================


def test_declining_one_figure_does_not_license_another_in_the_same_sentence():
    """FATAL. `_price` checks only `claim.value`, but `_coverage` folds the
    offer fact's WHOLE key set into the exemption — so every number the buyer
    mentioned went uncited:

        "I can't do $300.00, but these go for $6,200.00."   -> PASSED

    One legitimate decline, one fabricated price, zero violations, on a lot with
    no price fact at all.
    """
    r = run("I can't do $300.00, but these go for $6,200.00.",
            Claim(type=ClaimType.PRICE, value="300", source_fact_id="fo",
                  quote="I can't do $300.00"),
            facts=(offer_fact(300.0, 6200.0),))
    assert blocked(r), codes(r)


def test_a_claim_with_no_figure_cannot_smuggle_one():
    """`stated = _numbers(claim.value)` is empty when the value has no digits, so
    the misquote check was vacuously satisfied and `_denies` trivially true."""
    r = run("These are not cheap; $6,200.00 is the going rate.",
            Claim(type=ClaimType.PRICE, value="not cheap", source_fact_id="fo",
                  quote="These are not cheap"),
            facts=(offer_fact(6200.0),))
    assert blocked(r), codes(r)


def test_a_negation_token_is_not_a_decline():
    """FATAL. `_denies` is `bool(_NEGATION.search(clause))`, so an ENDORSEMENT
    containing a negation token read as a refusal:

        "I can't argue with $6,200.00 as the going rate."   -> PASSED

    The guard was decided by punctuation rather than meaning."""
    for text in ("I can't argue with $6,200.00 as the going rate.",
                 "No question about it, $6,200.00 is the going rate."):
        r = run(text,
                Claim(type=ClaimType.PRICE, value="$6,200.00",
                      source_fact_id="fo", quote=text),
                facts=(offer_fact(6200.0),))
        assert blocked(r), f"{text!r} -> {codes(r)}"


# =====================================================================
# Minting: the gate, in both directions
# =====================================================================


@pytest.mark.parametrize("intent,mints", [
    (Intent.NEGOTIATION, True),
    (Intent.PRICE_VALUE_Q, True),
    (Intent.AVAILABILITY_Q, False),
    (Intent.AUTHENTICITY_Q, False),
    (Intent.UNKNOWN, False),
])
def test_an_offer_is_minted_only_where_an_offer_is_plausible(intent, mints):
    """`offer__intent_gate_deleted` survived. `PRICE` is wanted by
    `availability_q`, `authenticity_q` and `unknown` — and `unknown` is where
    `prompt_injection` routes — so before the gate, every number in any message
    was minted as something the reply could cite."""
    ev = assemble(intent=intent,
                  resolution=get_resolver().resolve("would you take 320 for it"),
                  catalog=CAT, lot=CAT.lots["lot_006"])
    got = any(f.subject == "buyer" for f in ev.facts)
    assert got is mints


def test_the_minted_fact_is_observational_never_a_record():
    """`offer__fact_never_minted` survived. The authority is the whole point: a
    buyer saying a number does not make it true of anything, so a claim using it
    to assert a fact about the card mis-cites."""
    ev = assemble(intent=Intent.NEGOTIATION,
                  resolution=get_resolver().resolve("would you take 320"),
                  catalog=CAT, lot=CAT.lots["lot_006"])
    off = next(f for f in ev.facts if f.subject == "buyer")
    assert off.authority is Authority.OBSERVATIONAL
    assert off.value["is_offer"] is True


def test_the_offer_fact_cannot_back_a_grade_or_a_bid():
    """It is `kind=PRICE`, so `_require_kind` stops it standing in for anything
    else — which is the only part of `_offer`'s docstring that was ever true."""
    for kind in (ClaimType.GRADE, ClaimType.BID, ClaimType.POP):
        r = run("It's a PSA 10.",
                Claim(type=kind, value="10", source_fact_id="fo",
                      quote="It's a PSA 10"),
                facts=(offer_fact(10.0),))
        assert "mis_citation" in codes(r), kind.value


# =====================================================================
# B-126 — the vocabulary, in both directions at once
#
# Widening `_REFUSE_BEFORE` to catch more natural declines immediately
# unblocked "these **go for** $6,200" — because `go` appears in both an offer
# ("let it go for") and a price quotation. The same happened to `_REFUSE_VERB`
# and "the **going** rate". These pin both sides so the next widening cannot
# trade one for the other silently, which is this file's documented failure.
# =====================================================================


@pytest.mark.parametrize("text", [
    "I'm not able to do $300.00 on this one.",
    "$300.00 is below where we are, sorry.",
    "We're not taking $300.00 today.",
    "That's under my floor, $300.00 doesn't work.",
])
def test_natural_declines_are_not_blocked(text: str):
    """A refusal PATTERN is sufficient on its own. Requiring a negation token
    as well rejected "below where we are, sorry" — and "sorry" is not a
    negation, which is exactly why B-58 removed apologies from `_NEGATION`."""
    r = run(text,
            Claim(type=ClaimType.PRICE, value="300", source_fact_id="fo",
                  quote=text),
            facts=(offer_fact(300.0),))
    assert not blocked(r), codes(r)


@pytest.mark.parametrize("text", [
    "Yes — these go for $6,200.00.",
    "The going rate is $6,200.00 on these.",
    "I can't argue with $6,200.00 as the going rate.",
])
def test_a_price_quotation_is_never_read_as_a_refusal(text: str):
    """`go for` and `the going rate` are the two commonest ways to STATE a
    price in this domain, and both were briefly read as declines."""
    r = run(text,
            Claim(type=ClaimType.PRICE, value="$6,200.00", source_fact_id="fo",
                  quote=text),
            facts=(offer_fact(6200.0),))
    assert blocked(r), f"{text!r} -> {codes(r)}"
