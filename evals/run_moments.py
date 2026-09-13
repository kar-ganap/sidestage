"""Suite E — moment detection, scored on real bid data.

    uv run python -m evals.run_moments
    uv run python -m evals.run_moments --sweep     # show the threshold surface

WHAT MAKES THIS SUITE UNUSUAL, and it is the honest part: **there is no model
here**. `app/moments.py` is arithmetic on two numbers, so this does not measure a
classifier — it measures whether two thresholds are in the right place. Anyone
can recompute every call by hand from the table in the observation doc.

WHAT IT IS ACTUALLY WORTH. The labels are real bid movement from a live show,
recorded by an observer before any classifier existed, which is a stronger
footing than annotation. But **n = 10, with exactly one stalled instance**, and
that is reported here rather than smoothed into an average — a rule whose
negative class rests on a single observation should say so every time it is
scored.

THE SWEEP, AND WHAT IT DOES NOT SHOW. `--sweep` reports every threshold pair
that scores 10/10, and the band is wide: extensions 3-6 crossed with movement
5-30% all score perfectly. The flattering reading is "the placement is robust".
The honest one is that **a sweep where everything passes is not evidence the
thresholds are right — it is evidence this suite cannot tell them apart.**

The observed extension counts are 1, 2, 3, 3, 6, 7, 7, 11, 15, 24. Nothing sits
between 3 and 6, so any cutoff inside that gap separates the same two groups.
The suite therefore validates the *shape* of the rule — that a gap exists and
that extension count finds it — and says nothing about whether 5 beats 4. With
ten lots it cannot, and quoting the band as validation of the constants would be
reading the data backwards.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.moments import (
    HOT_DELTA_PCT,
    HOT_EXTENSIONS,
    STALL_EXTENSIONS,
    Moment,
    classify,
    nudge,
)

DATA = Path(__file__).parent / "data"
EBAY_LOTS = Path(__file__).parent.parent / "docs" / "research" / "ebaylive_lots.tsv"


def load() -> list[dict]:
    rows = [json.loads(l) for l in
            (DATA / "moments_labelled.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    return [r for r in rows if "_meta" not in r]


def call_for(r: dict, *, hot_ext: int = HOT_EXTENSIONS,
             hot_pct: float = HOT_DELTA_PCT, stall_ext: int = STALL_EXTENSIONS):
    import app.moments as M
    old = (M.HOT_EXTENSIONS, M.HOT_DELTA_PCT, M.STALL_EXTENSIONS)
    M.HOT_EXTENSIONS, M.HOT_DELTA_PCT, M.STALL_EXTENSIONS = hot_ext, hot_pct, stall_ext
    try:
        return classify(extensions=r["extensions"],
                        bid_at_first_extension=r["bid_at_first_extension"],
                        current_bid=r["final_bid"])
    finally:
        M.HOT_EXTENSIONS, M.HOT_DELTA_PCT, M.STALL_EXTENSIONS = old


def score(rows: list[dict], **kw) -> tuple[int, list[tuple[dict, str]]]:
    ok, wrong = 0, []
    for r in rows:
        got = call_for(r, **kw).moment.value
        if got == r["expected"]:
            ok += 1
        else:
            wrong.append((r, got))
    return ok, wrong


def ebay_check() -> None:
    """An independent instance of the stalled case, from a different platform.

    D-26b flagged that the stall branch rested on ONE observation. The eBay Live
    capture supplies a second: lot 263 opened at $1 and closed at $1. It is not
    labelled ground truth — extension counts were not visible in that UI — so it
    is reported as corroboration rather than folded into the score.
    """
    if not EBAY_LOTS.exists():
        return
    with EBAY_LOTS.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    by: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("lot"):
            by.setdefault(r["lot"], []).append(r)
    def secs(c: str) -> int:
        m, s2 = (c or "0:00").split(":")
        return int(m) * 60 + int(s2)

    flat = []
    for lot, rs in by.items():
        prices = [float(x["price"]) for x in rs if x.get("price")]
        if not prices or max(prices) != min(prices):
            continue
        # The lot must have been OBSERVED, not joined at its tail. Lot 257 is
        # flat across every frame and is not a stall: the capture starts at
        # clock 0:02, so we saw two seconds of a lot that had already run. A
        # flat price over frames we were not present for is not evidence of
        # anything, and counting it would have inflated a corroboration that
        # exists to shore up a single-instance branch.
        first_clock = secs(rs[0].get("clock", "0:00"))
        if first_clock < 10:
            continue
        dur = int(rs[-1]["sec"]) - int(rs[0]["sec"])
        flat.append((lot, prices[0], dur, first_clock))
    if not flat:
        return
    print("\n   corroboration — eBay Live, a different platform and seller")
    for lot, price, dur, clk in flat:
        print(f"      lot {lot}: ${price:,.0f} unchanged over {dur}s, "
              f"first seen with {clk}s on the clock")
    print("      NOT scored: extension counts are not visible in that UI, so this")
    print("      is evidence the case is real, not evidence the threshold is right.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    rows = load()

    print("=" * 78)
    print("SUITE E — moment detection on 10 lots of real bid data")
    print("=" * 78)
    print(f"\n   rule: hot if extensions >= {HOT_EXTENSIONS} and movement >= "
          f"{HOT_DELTA_PCT:.0%};  stalled if extensions >= {STALL_EXTENSIONS} "
          f"and movement == 0")
    print("   no model — two numbers and two thresholds\n")

    print(f"   {'lot':<32}{'ext':>4}{'delta':>9}{'%':>7}  {'expected':<9}{'got':<9}")
    for r in rows:
        c = call_for(r)
        mark = "  " if c.moment.value == r["expected"] else " X"
        print(f"   {r['title'][:30]:<32}{r['extensions']:>4}"
              f"{c.delta:>9,.0f}{c.delta_pct:>7.0%}  "
              f"{r['expected']:<9}{c.moment.value:<9}{mark}")

    ok, wrong = score(rows)
    print(f"\n   {ok}/{len(rows)} correct")
    for r, got in wrong:
        print(f"      MISS  {r['title']}: expected {r['expected']}, got {got}")

    # per-class, because 6/10 of this set is one class
    for m in Moment:
        sub = [r for r in rows if r["expected"] == m.value]
        if not sub:
            continue
        hit = sum(1 for r in sub if call_for(r).moment.value == m.value)
        flag = "   <- ONE instance; no rate should be quoted" if len(sub) == 1 else ""
        print(f"      {m.value:<9}{hit}/{len(sub)}{flag}")

    print("\n   the nudge each hot/stalled lot would produce")
    for r in rows:
        c = call_for(r)
        n = nudge(c, lot_title=r["title"])
        if n:
            print(f"      {r['title'][:30]:<32}{n}")

    if a.sweep:
        print("\n   threshold sweep — is the placement a property of the data?")
        print(f"      {'hot_ext':>8}{'hot_pct':>9}{'correct':>9}")
        for he in range(3, 9):
            for hp in (0.05, 0.10, 0.15, 0.20, 0.30):
                s, _ = score(rows, hot_ext=he, hot_pct=hp)
                star = "  <- shipped" if (he == HOT_EXTENSIONS and
                                          abs(hp - HOT_DELTA_PCT) < 1e-9) else ""
                if s == len(rows):
                    print(f"      {he:>8}{hp:>9.0%}{s:>9}{star or ''}")
        print("      Every row above scores 10/10. Read that as the suite being")
        print("      unable to separate these thresholds, not as the shipped pair")
        print("      being validated: observed extensions are 1,2,3,3,6,7,7,11,15,24")
        print("      and any cutoff in the empty 3..6 gap splits the same two groups.")

    ebay_check()
    print()
    return 0 if not wrong else 1


if __name__ == "__main__":
    raise SystemExit(main())
