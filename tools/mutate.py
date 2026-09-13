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

SUITE = ["tests/test_verify.py", "tests/test_stats.py"]


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="substring filter on mutant name")
    a = ap.parse_args()
    chosen = {k: v for k, v in MUTANTS.items() if a.k in k}

    killed, survived = [], []
    for name, apply in chosen.items():
        restore()
        apply()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = pytest.main([*SUITE, "-x", "-q", "--no-header",
                              "-p", "no:cacheprovider", "-p", "no:randomly"])
        (killed if rc != 0 else survived).append(name)
    restore()

    print(f"\n  {len(killed)}/{len(chosen)} mutants killed\n")
    for m in sorted(survived):
        print(f"     SURVIVED  {m}")
    if survived:
        print(f"\n  A surviving mutant means NO test constrains that rule.")
        print(f"  The suite is green with it deleted.\n")
    else:
        print("  every rule is constrained by at least one test\n")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
