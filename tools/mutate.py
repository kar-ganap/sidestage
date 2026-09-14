#!/usr/bin/env python3
"""Delete a rule, run the suite, see whether anything goes red.

    uv run python tools/mutate.py              # all mutants
    uv run python tools/mutate.py -k registry  # a subset

WHY (B-105). `docs/SUBMISSION.md` cites mutation testing as one of two pillars of
"what caught the bugs review-by-reading missed", and `BUILD-LOG.md` B-70 reports
"15/15 mutants killed". The harness that produced those numbers lived in a
scratch directory outside the repository. A reviewer could not run it, and the
claim was therefore an assertion in a document about not making assertions.

A later pass found **13 of 48 mutants surviving** the same suite — including
deleting `observational_assertion`, which is D-12, the entire authority model.
B-70's "15/15" was true of the 15 mutants it happened to define.

WHAT A MUTANT IS. A one-line disabling of a rule. If the suite still passes, no
test constrains that rule and the coverage is decorative. The question a test
should answer is not "does it pass" but "what would have to break for it to
fail", and the cheapest way to answer that is to break the thing on purpose.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app.verify as V  # noqa: E402
from app.models import ClaimType, Severity, Violation  # noqa: E402

# B-121: the reviewer ran mutants against the FULL suite, which is strictly
# stronger — a rule may be constrained by a test in another file, and scoping
# the harness to two files made it report kills it had not earned.
SUITE = ["tests/"]


def _kind_only(kind: ClaimType):
    """Keep the mis-citation guard, delete everything the verifier does after."""
    def f(claim, fact, ctx):
        return V._require_kind(claim, fact, kind, "wrong kind") or []
    return f


def _policy_kind_only(claim, fact, ctx):
    if fact.kind not in (ClaimType.SHIPPING, ClaimType.RETURNS,
                         ClaimType.AUTHENTICITY):
        return [V._miscite(claim, fact, "a policy claim must cite a policy fact")]
    return []


def _no_misquote(original):
    """`_price` with the `offer_misquoted` check removed."""
    def f(claim, fact, ctx):
        out = original(claim, fact, ctx)
        return [v for v in out if v.code != "offer_misquoted"]
    return f


def _no_offer_branch(original):
    """`_price` with the whole B-111 offer branch deleted — the mutant that
    flips the flagship repro from blocked to pass."""
    def f(claim, fact, ctx):
        if isinstance(fact.value, dict) and fact.value.get("is_offer"):
            return []
        return original(claim, fact, ctx)
    return f


def _denies_any(quote, value):
    """`_denies` with `all` reverted to `any` over occurrences (B-118) — repeat
    the value's words in an earlier negated clause and the later assertion is
    exempt."""
    import re as _re
    q = V._soft(quote)
    nums = {V._numkey(m.group(0)) for m in V._NUMBER_RUN.finditer(V._soft(value))}
    spans = [(m.start(), m.end()) for m in V._NUMBER_RUN.finditer(q)
             if V._numkey(m.group(0)) in nums]
    for w in (x for x in V._WORD.findall(V._soft(value))
              if len(x) > 1 and not x.isdigit()):
        spans += [(m.start(), m.end()) for m in _re.finditer(_re.escape(w), q)]
    return any(V._negated_at(q, a, b) for a, b in spans) if spans else False


def _identity_no_ambiguity(claim, fact, ctx):
    import re
    if (v := V._require_kind(claim, fact, ClaimType.IDENTITY, "x")):
        return v
    known = {t for val in fact.value.values()
             for t in re.findall(r"[a-z0-9]+", V._norm(str(val)))}
    unknown = [t for t in re.findall(r"[a-z0-9]+", V._norm(claim.value))
               if t not in known and len(t) > 1]
    return [Violation(code="identity_mismatch", severity=Severity.UNREPAIRABLE,
                      message="x", actual=claim.value)] if unknown else []


# name -> a callable that applies the mutation. Grouped by what it deletes.
MUTANTS: dict[str, object] = {
    # whole passes
    "coverage_off":       lambda: setattr(V, "_coverage", lambda d, c: []),
    "lexical_off":        lambda: setattr(V, "_lexical", lambda d, c: []),
    "staleness_off":      lambda: setattr(V, "_staleness", lambda e, c, n: []),
    "operator_only_off":  lambda: setattr(V, "_operator_only", lambda d, c: []),
    "dedupe_off":         lambda: setattr(V, "_dedupe", lambda v: v),
    # The register pass. It does not fire on the recorded corpus by design —
    # the model already complies — so without a mutant it would be a rule no
    # test constrains, which is exactly B-127's finding.
    "tone_off":           lambda: setattr(V, "_tone", lambda d, c: []),
    "structural_off":     lambda: setattr(V, "_structural", lambda i, c, x: []),
    # the helpers the coverage rewrite rests on
    "numkey_id":          lambda: setattr(V, "_numkey", lambda x: x),
    "lemma_id":           lambda: setattr(V, "_lemma", lambda w: w),
    "proper_nouns_off":   lambda: setattr(V, "_proper_nouns", lambda c: set()),
    "deferred_off":       lambda: setattr(V, "_deferred", lambda s: set()),
    "negated_never":      lambda: setattr(V, "_negated_at", lambda *a, **k: False),
    "negated_always":     lambda: setattr(V, "_negated_at", lambda *a, **k: True),
    "clause_whole":       lambda: setattr(V, "_clause_around",
                                          lambda sen, s, e: sen),
    "overlaps_always":    lambda: setattr(V, "_overlaps", lambda q, s: True),
    "fact_keys_raw":      lambda: setattr(V, "_fact_keys",
                                          lambda f: V._keys(f"{f.value} {f.note}")),
    "denies_whole":       lambda: setattr(V, "_denies",
                                          lambda q, v: V._is_negated(q)),
    "near_quote_whole":   lambda: setattr(V, "_near_quote",
                                          lambda r, q, lookahead=1: r),
    "asserts_absence_true": lambda: setattr(V, "_asserts_absence", lambda c: True),
    # B-130's two failure directions, one mutant each. `_none` is the
    # over-permissive one — every decline exempt, so a price smuggled into a
    # denial passes. `_all` is the over-strict one B-24 and B-130 both hit:
    # any number at all, including the window the fact itself names, blocks the
    # reply the rule's own message prescribes.
    "foreign_numbers_none": lambda: setattr(
        V, "_foreign_numbers", lambda q, f: set()),
    "foreign_numbers_all": lambda: setattr(
        V, "_foreign_numbers", lambda q, f: set(V._numbers(q))),
    "is_deferral_true":   lambda: setattr(V, "_is_deferral", lambda q: True),
    # per-type rules, reduced to their kind guard
    **{f"{k.value}_kindonly": (lambda kk=k: V.REGISTRY.__setitem__(kk, _kind_only(kk)))
       for k in (ClaimType.GRADE, ClaimType.COMP, ClaimType.POP,
                 ClaimType.CENTERING, ClaimType.CONDITION,
                 ClaimType.AVAILABILITY, ClaimType.PRICE, ClaimType.BID,
                 ClaimType.VARIANT, ClaimType.IDENTITY)},
    "policy_kindonly":    lambda: [V.REGISTRY.__setitem__(k, _policy_kind_only)
                                   for k in (ClaimType.SHIPPING, ClaimType.RETURNS,
                                             ClaimType.AUTHENTICITY)],
    "identity_amb_off":   lambda: V.REGISTRY.__setitem__(ClaimType.IDENTITY,
                                                         _identity_no_ambiguity),
    # --- B-121: mutants an ADVERSARY defined, not the author ---------------
    #
    # This harness reported "32/32 killed" while an independent reviewer wrote
    # 19 mutants it does not define and **15 survived** — including deleting the
    # entire `_price` offer branch, which flips the flagship repro from blocked
    # to pass. That is the same sentence this file's docstring uses to condemn
    # the harness before it: a mutant list written by whoever wrote the fixes
    # covers the rules they were thinking about.
    #
    # These are that reviewer's survivors, adopted verbatim. The lesson is not
    # "the list is now complete" — it is that the list must keep coming from
    # somewhere other than the person it is grading.
    # Dispatched through REGISTRY, so `setattr(V, "_price", ...)` is a NO-OP —
    # `verify()` reads `REGISTRY.get(claim.type)`, which still holds the
    # original function object. B-122: six mutants "survived" for this reason
    # and sent me writing tests for rules that were never disabled.
    "offer__price_branch_off": lambda: V.REGISTRY.__setitem__(
        ClaimType.PRICE, _no_offer_branch(_REGISTRY[ClaimType.PRICE])),
    "offer__refuses_off":      lambda: setattr(V, "_refuses", lambda q, v: True),
    "offer__misquote_off":     lambda: V.REGISTRY.__setitem__(
        ClaimType.PRICE, _no_misquote(_REGISTRY[ClaimType.PRICE])),
    "soft__drops_delimiters":  lambda: setattr(
        V, "_soft", lambda x: V._norm(x)),
    "interjection__never":     lambda: setattr(
        V, "_INTERJECTION", __import__("re").compile(r"(?!x)x")),
    "interjection__any_comma_word": lambda: setattr(
        V, "_INTERJECTION", __import__("re").compile(r",\s*\w+\s*,")),
    "clause__dashes_removed":  lambda: setattr(
        V, "_CLAUSE", __import__("re").compile(r"[,;:]")),
    "denies__any_occurrence":  lambda: setattr(V, "_denies", _denies_any),
    "negated_at__whole_string": lambda: setattr(
        V, "_clause_around", lambda sen, a, b: sen),
    "sizing_repairable":  lambda: V.REGISTRY.__setitem__(
        ClaimType.SIZING,
        lambda c, f, x: V._require_kind(c, f, ClaimType.SIZING, "no sizing") or []),
}

_SAVED = {n: getattr(V, n) for n in dir(V) if not n.startswith("__")}
_REGISTRY = dict(V.REGISTRY)


def restore() -> None:
    V.REGISTRY.clear()
    V.REGISTRY.update(_REGISTRY)
    for name, obj in _SAVED.items():
        try:
            setattr(V, name, obj)
        except Exception:                     # noqa: BLE001 — read-only members
            pass


def _probe() -> tuple:
    """A behavioural fingerprint: the verdicts of a spread of inputs.

    Wide on purpose — it has to be sensitive to every rule a mutant can touch,
    so it exercises the offer path, the clause splitter, the negation window,
    coverage, and the per-type registry.
    """
    from datetime import UTC, datetime
    from app.catalog import get_catalog
    from app.models import Authority, Claim, Draft, Evidence, Fact

    now = datetime(2026, 3, 1, tzinfo=UTC)
    cat = get_catalog()

    def fact(fid, kind, value, auth=Authority.RECORD, note="n"):
        return Fact(id=fid, kind=kind, subject="lot_t", value=value,
                    authority=auth, source="t", as_of=now, note=note)

    offer = fact("fo", ClaimType.PRICE,
                 {"buyer_said": [300.0, 6200.0], "operator_only": False,
                  "is_offer": True}, Authority.OBSERVATIONAL,
                 "the buyer named $300.00, $6,200.00")
    variant = fact("fv", ClaimType.VARIANT,
                   {"printed": ["unlimited"], "language": "en"},
                   Authority.CATALOG, "Champion's Path (en) printed: unlimited")
    grade = fact("fg", ClaimType.GRADE,
                 {"grader": "RAW", "value": None, "cert": None, "raw": True})

    cases = [
        ("Yes — these go for $6,200.00.",
         [Claim(ClaimType.PRICE, "$6,200.00", "fo", "these go for $6,200.00")],
         (offer,)),
        ("I can't do $300.00, but these go for $6,200.00.",
         [Claim(ClaimType.PRICE, "300", "fo", "I can't do $300.00")], (offer,)),
        ("I can't argue with $6,200.00 as the going rate.",
         [Claim(ClaimType.PRICE, "$6,200.00", "fo",
                "I can't argue with $6,200.00")], (offer,)),
        ("These are not cheap; $6,200.00 is the going rate.",
         [Claim(ClaimType.PRICE, "not cheap", "fo", "These are not cheap")],
         (offer,)),
        # An EM dash, not an ASCII hyphen: `_norm` happens to keep `-`, so a
        # hyphen probe made `soft__drops_delimiters` look like a no-op and the
        # harness reported it `unverified` (B-127).
        ("This is 1st Edition \u2014 no doubt about it.",
         [Claim(ClaimType.VARIANT, "1st Edition", "fv",
                "This is 1st Edition \u2014 no doubt about it.")], (variant,)),
        ("It is not, however, 1st Edition.",
         [Claim(ClaimType.VARIANT, "1st Edition", "fv",
                "It is not, however, 1st Edition.")], (variant,)),
        ("No 1st Edition copies were reprinted so this 1st Edition is genuine.",
         [Claim(ClaimType.VARIANT, "1st Edition", "fv",
                "No 1st Edition copies were reprinted so this 1st Edition "
                "is genuine.")], (variant,)),
        ("We can't cover postage, however, the card is mint.", [], (grade,)),
        ("It grades a gem mint.", [], (grade,)),
    ]
    out = []
    for text, claims, facts in cases:
        ev = Evidence(id="e", lot_id=None, facts=facts, built_at=now,
                      assembly_ms=0)
        d = Draft(id="d", card_id="c", text=text, claims=claims,
                  evidence_id="e", model="t", attempt=0, created_at=now)
        r = V.verify(d, ev, catalog=cat, now=now)
        out.append((r.verdict.value, tuple(sorted(v.code for v in r.violations))))
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="substring filter on mutant name")
    a = ap.parse_args()
    chosen = {k: v for k, v in MUTANTS.items() if a.k in k}

    killed, survived, unverified = [], [], []
    for name, apply in chosen.items():
        restore()
        base = _probe()
        apply()
        moved = _probe() != base
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = pytest.main([*SUITE, "-x", "-q", "--no-header",
                              "-p", "no:cacheprovider", "-p", "no:randomly"])
        if rc != 0:
            killed.append(name)
        elif moved:
            survived.append(name)          # real gap: it bit, no test noticed
        else:
            unverified.append(name)        # the probe could not see it bite
    restore()

    # B-125. The denominator is EVERY mutant attempted, never a filtered
    # subset. The previous version excluded anything `_probe()` could not see
    # change and called it "inert" — and the probe was nine hand-picked cases,
    # so it could not see the reserve leak, the lexical pass, staleness or the
    # structural pass. **23 of 41 mutants were silently removed from the
    # denominator**, including several the harness had killed in an earlier run.
    #
    # That is strictly worse than the problem it was added to solve: B-122's
    # inert mutants inflated the SURVIVED list, and this inflated the SCORE. A
    # probe narrow enough to miss a rule is a probe narrow enough to launder it.
    print(f"\n  {len(killed)}/{len(chosen)} mutants killed\n")
    for m in sorted(survived):
        print(f"     SURVIVED    {m}   <- it bit, and no test noticed")
    for m in sorted(unverified):
        print(f"     unverified  {m}   <- suite green, and the probe could not "
              f"see it bite; may be a gap OR a broken mutant")
    if survived:
        print("\n  A SURVIVED mutant bit and no test noticed: a real gap.")
    if unverified:
        print("\n  An `unverified` mutant is NOT a pass. Either it disables a")
        print("  rule the probe cannot reach, or the mutant patches the wrong")
        print("  dispatch point — both need a person to look (B-125).")
    if not survived and not unverified:
        print("  every rule is constrained by at least one test\n")
    return 1 if (survived or unverified) else 0


if __name__ == "__main__":
    raise SystemExit(main())
