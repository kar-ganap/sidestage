"""Entity resolution, tested against what people actually typed.

Every case in `test_observed_failure_modes` is a verbatim string from
`evals/data/triage_*.jsonl` — 477 messages of real Whatnot chat. None of them
were invented to make the resolver look good, which is the point: the seventeen
ways exact matching breaks were discovered, not designed.
"""

from __future__ import annotations

import time

import pytest

from app.catalog import get_catalog
from app.entities import FUZZY_MIN, EntityResolver, MatchKind


@pytest.fixture(scope="module")
def r() -> EntityResolver:
    return EntityResolver(get_catalog())


# =====================================================================
# The seventeen observed failure modes
# =====================================================================


@pytest.mark.parametrize(
    "text, expect_name, kind",
    [
        # nickname — from the table seeded out of real chat
        ("Zard", "Charizard", MatchKind.ALIAS),
        ("big boy gengar", "Gengar", MatchKind.ALIAS),
        ("shining dragon", "Shining Dragonite", MatchKind.ALIAS),
        # exact
        ("Blaziken ex", "Blaziken ex", MatchKind.EXACT),
        # plural — "You got any psyducks"
        ("Any Blazikens ?", "Blaziken ex", MatchKind.PLURAL),
        # set narrows a card: two weak signals combining into one strong one
        ("gengar fire red", "Gengar", MatchKind.EXACT),
    ],
)
def test_observed_failure_modes(r, text, expect_name, kind):
    res = r.resolve(text)
    assert res.matches, f"no match for {text!r}"
    assert res.matches[0].kind is kind
    assert any(i.name == expect_name for i in res.items), \
        f"{text!r} -> {[i.name for i in res.items]}, wanted {expect_name}"


def test_misspelling_resolves_by_fuzzy(r):
    """`dragonight` was typed by a real viewer asking about back-wall stock."""
    res = r.resolve("dragonight")
    assert res.matches
    assert all("dragonite" in i.name.lower() for i in res.items)


def test_qualifier_noise_is_stripped_not_matched(r):
    """"bubble mew" — `bubble` is a descriptor, not a catalog attribute."""
    res = r.resolve("bubble mew")
    assert res.items, "the Mew should still resolve"
    assert not any(m.surface == "bubble" for m in res.matches)


def test_non_catalog_words_match_nothing(r):
    for text in ("Go quicker", "im hungry", "Back is clean ?", "wtf lmaoooo"):
        res = r.resolve(text)
        assert not res.items, f"{text!r} wrongly matched {[i.name for i in res.items]}"
        assert not res.mentions_catalog


def test_cards_absent_from_catalog_are_not_forced(r):
    """`rakwaza` is Rayquaza, which we do not stock. Matching it to the nearest
    card we DO stock would be the expensive kind of wrong."""
    for text in ("rakwaza", "venasaur", "psyducks"):
        assert not r.resolve(text).items


# =====================================================================
# Abstention — D-14
# =====================================================================


@pytest.mark.parametrize("text", ["mew", "the mew one", "bubble mew"])
def test_family_name_abstains_rather_than_picking(r, text):
    """Two Mews sit in the catalog on purpose. A resolver that picks one is
    worse than one that asks, because a wrong pick is indistinguishable from a
    right one until money has moved."""
    res = r.resolve(text)
    assert res.needs_clarification
    assert not res.resolved
    assert "which" in res.clarification.lower()
    assert len(res.items) > 1


def test_clarification_names_distinguishing_attributes(r):
    """The operator reads this aloud, so it must contain words a buyer can act
    on — not internal ids."""
    c = r.resolve("mew").clarification
    assert "itm_" not in c and "lot_" not in c
    assert "Mew" in c


def test_unambiguous_match_does_not_abstain(r):
    res = r.resolve("Blaziken ex")
    assert res.resolved and not res.needs_clarification


# =====================================================================
# Explainability — D-15
# =====================================================================


def test_every_match_explains_itself(r):
    for text in ("Zard", "dragonight", "Any Blazikens ?", "gengar fire red"):
        res = r.resolve(text)
        for m in res.matches:
            assert m.why and not m.why.startswith("None")
        assert res.explain()


def test_catalog_mention_is_an_intent_signal_without_syntax(r):
    """Half of observed requests carry no question mark. Naming something for
    sale is the signal; punctuation is not."""
    assert r.resolve("lugia next!").mentions_catalog is False   # not in catalog
    assert r.resolve("Blaziken ex").mentions_catalog is True
    assert r.resolve("Zard").mentions_catalog is True


# =====================================================================
# The length prune is sound, not heuristic
# =====================================================================


def test_length_prune_never_drops_a_reachable_match(r):
    """The prune claims that a surface outside [n/s, n*s] cannot reach
    FUZZY_MIN. Verify that directly against the real vocabulary rather than
    trusting the algebra."""
    from difflib import SequenceMatcher

    span = 2.0 / FUZZY_MIN - 1.0
    surfaces = list(r.vocab.name_index)
    for probe in ("dragonight", "charizardd", "blazikenex", "shiningdragonite"):
        n = len(probe)
        for s in surfaces:
            if n / span <= len(s) <= n * span:
                continue
            ratio = SequenceMatcher(None, probe, s).ratio()
            assert ratio < FUZZY_MIN, (
                f"prune would have dropped {s!r} at ratio {ratio:.3f} for {probe!r}"
            )


# =====================================================================
# Latency — D-15 budgets the cheap arm at <5 ms per message
# =====================================================================


def test_resolution_stays_inside_the_cheap_arm_budget(r):
    corpus = [
        "mew", "Zard", "dragonight", "Any Blazikens ?", "gengar fire red",
        "run the shining dragon", "Go quicker", "im hungry", "wtf lmaoooo",
        "Back is clean ?", "team rocket holos", "big boy gengar",
    ]
    for m in corpus:          # warm the interpreter, not the measurement
        r.resolve(m)
    t0 = time.perf_counter()
    reps = 40
    for _ in range(reps):
        for m in corpus:
            r.resolve(m)
    mean_ms = (time.perf_counter() - t0) / (reps * len(corpus)) * 1000
    assert mean_ms < 5.0, f"mean {mean_ms:.2f} ms exceeds the 5 ms cheap-arm budget"
