"""Evidence assembly — the half of D-09 that makes verification a lookup.

The properties worth testing here are not "does it fetch things" but the three
that the rest of the system leans on:

  1. the model's universe is bounded — nothing assertable is absent, nothing
     absent is assertable;
  2. refusal is citable rather than implied;
  3. the snapshot decays, and saying so is a violation rather than a silent
     update.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.catalog import get_catalog
from app.entities import get_resolver
from app.evidence import assemble, refresh
from app.models import Authority, ClaimType, Intent


@pytest.fixture(scope="module")
def cat():
    return get_catalog()


@pytest.fixture(scope="module")
def res():
    return get_resolver()


def ev_for(cat, res, intent, text, lot_id=None):
    return assemble(intent=intent, resolution=res.resolve(text),
                    catalog=cat, lot=cat.lots.get(lot_id) if lot_id else None)


# =====================================================================
# The demo case
# =====================================================================


def test_variant_carries_both_record_and_catalog_authority(cat, res):
    """The Champion's Path block needs two facts, not one.

    A record check alone cannot say a claimed variant is IMPOSSIBLE; a catalog
    check alone passes a card that could have been 1st Edition but isn't. The
    verifier needs both to tell those apart.
    """
    ev = ev_for(cat, res, Intent.ATTRIBUTE_Q, "is that 1st edition", "lot_007")
    variants = ev.of_kind(ClaimType.VARIANT)
    authorities = {f.authority for f in variants}
    assert Authority.RECORD in authorities
    assert Authority.CATALOG in authorities

    catalog_fact = next(f for f in variants if f.authority is Authority.CATALOG)
    assert "1st_edition" not in catalog_fact.value["printed"], \
        "Champion's Path printed no English 1st Edition — this is the whole demo"
    assert catalog_fact.value["language"] == "en"


def test_comp_that_fails_the_rule_says_why(cat, res):
    """A blocked comp explains itself in the catalog's words, so the operator
    sees a reason rather than a refusal."""
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "how much", "lot_006")
    comp = ev.of_kind(ClaimType.COMP)[0]
    assert comp.value["quotable"] is False
    assert "3 sales" in comp.value["reason"]
    assert comp.value["phrase"] == "", "a non-quotable comp must not carry a phrase"


def test_quotable_comp_renders_as_a_range_with_n(cat, res):
    """The stalled Pikachu. It sold at $111 while the room speculated a crash —
    and this is the fact that would have answered them."""
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "what did that go for", "lot_003")
    comp = ev.of_kind(ClaimType.COMP)[0]
    assert comp.value["quotable"] is True
    assert comp.value["n"] >= 5
    assert "last 6 sold" in comp.value["phrase"] and "past 90d" in comp.value["phrase"]


# =====================================================================
# Refusal is citable, not implied
# =====================================================================


def test_observational_attributes_get_a_fact_of_their_own(cat, res):
    """Asked five times in the observed corpus, answered zero times. The copilot
    cannot answer it either — but it can point at why, instead of improvising a
    hedge that default-deny would block."""
    ev = ev_for(cat, res, Intent.GRADE_CONDITION_Q, "is the back clean", "lot_006")
    obs = [f for f in ev.facts if f.authority is Authority.OBSERVATIONAL]
    assert obs, "a raw card must carry an observational marker"
    f = obs[0]
    assert f.value["assertable"] is False
    assert "back_condition" in f.value["observational"]
    assert "defer to the host" in f.note


def test_slab_narrows_what_is_observational(cat, res):
    """In a sealed slab the back and edges are visible through the case, so the
    refusal should be narrower than for a raw card — over-refusing is its own
    failure."""
    raw = ev_for(cat, res, Intent.GRADE_CONDITION_Q, "condition", "lot_006")
    slab = ev_for(cat, res, Intent.GRADE_CONDITION_Q, "condition", "lot_007")
    raw_attrs = next(f for f in raw.facts if f.authority is Authority.OBSERVATIONAL).value
    slab_attrs = next(f for f in slab.facts if f.authority is Authority.OBSERVATIONAL).value
    assert "back_condition" in raw_attrs["observational"]
    assert "back_condition" not in slab_attrs["observational"]


def test_ambiguity_is_a_fact_the_model_can_cite(cat, res):
    """Two Mews and no instruction is how a model ends up picking one."""
    ev = assemble(intent=Intent.AVAILABILITY_Q,
                  resolution=res.resolve("do u have the mew one"), catalog=cat)
    amb = [f for f in ev.facts if isinstance(f.value, dict) and f.value.get("ambiguous")]
    assert amb, "an unresolved reference must surface as evidence"
    assert "which" in amb[0].value["question"].lower()
    assert len(amb[0].value["candidates"]) > 1
    assert ev.facts[0].id == amb[0].id, "ambiguity goes first — it governs everything after"


# =====================================================================
# The reserve is real, checkable, and must never be said out loud
# =====================================================================


def test_reserve_is_marked_operator_only(cat, res):
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "how much", "lot_006")
    reserve = [f for f in ev.of_kind(ClaimType.PRICE)
               if isinstance(f.value, dict) and "reserve" in f.value]
    assert reserve, "an auction lot with a reserve should expose it to the operator"
    assert reserve[0].value["operator_only"] is True
    assert "never quote" in reserve[0].note.lower()


# =====================================================================
# Bounded universe
# =====================================================================


def test_intent_bounds_what_is_assembled(cat, res):
    """A shipping question does not benefit from having comps in front of it —
    tokens cost money and spare material is somewhere to wander."""
    ship = ev_for(cat, res, Intent.SHIPPING_RETURNS_Q, "combined shipping", "lot_006")
    price = ev_for(cat, res, Intent.PRICE_VALUE_Q, "how much", "lot_006")
    assert not ship.of_kind(ClaimType.COMP)
    assert price.of_kind(ClaimType.COMP)


def test_every_fact_has_a_source_and_an_as_of(cat, res):
    """Provenance is what the trust chips render and what the ledger stores."""
    for intent in (Intent.ATTRIBUTE_Q, Intent.PRICE_VALUE_Q, Intent.AVAILABILITY_Q):
        ev = ev_for(cat, res, intent, "Any Blazikens ?")
        for f in ev.facts:
            assert f.source and f.as_of and f.note
            assert ev.by_id(f.id) is f, "ids must be unique and resolvable"


def test_assembly_is_off_the_critical_path(cat, res):
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "what did that go for", "lot_003")
    assert ev.assembly_ms < 50, f"assembly took {ev.assembly_ms} ms"


# =====================================================================
# The snapshot decays inside the request
# =====================================================================


def test_a_bid_that_moves_between_assembly_and_verify_is_a_violation(cat, res):
    """The concrete D-09 case. ~800 ms passes between drafting and verifying,
    and on a contested lot bids were observed landing every few seconds — so a
    reply quoting the assembly-time bid can be wrong by the time it is sent.
    """
    lot = cat.lots["lot_006"]
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "how much", "lot_006")

    # Nothing has moved yet, and the TTL has not elapsed.
    assert refresh(ev, catalog=cat) == []

    original = lot.current_bid
    try:
        lot.current_bid = (original or 0) + 25          # a bid lands
        later = datetime.now(UTC) + timedelta(seconds=5)
        moved = refresh(ev, catalog=cat, now=later)
        assert moved, "a moved bid must be reported"
        assert moved[0].was == original and moved[0].now == original + 25
    finally:
        lot.current_bid = original


def test_stale_facts_do_not_silently_update(cat, res):
    """`refresh` reports; it does not patch. The draft was written against the
    old value and its claims may no longer follow from the new one."""
    lot = cat.lots["lot_006"]
    ev = ev_for(cat, res, Intent.PRICE_VALUE_Q, "how much", "lot_006")
    bid_fact = ev.of_kind(ClaimType.BID)[0]
    original = lot.current_bid
    try:
        lot.current_bid = (original or 0) + 10
        refresh(ev, catalog=cat, now=datetime.now(UTC) + timedelta(seconds=5))
        assert bid_fact.value["current_bid"] == original, "evidence must be immutable"
    finally:
        lot.current_bid = original
