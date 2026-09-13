"""Suite F — is the responsiveness judge reliable enough to score anything?

    uv run python -m evals.run_judge              # from recorded runs, free
    uv run python -m evals.run_judge --live -n 40 # fresh test-retest, costs money

WHY THIS EXISTS (B-89). `app/judge.py` is the responsiveness axis of Spike 1's
ablation. It decides the `BOTH` column, and `BOTH` is the only axis on which the
shipped system beats a mute one — so every falsifiability argument in this
project rests on it.

Its entire validation was B-32: *"on 12 cases both models scored 12/12"*. Twelve
cases. Wilson 95% on 12/12 is **[75.7%, 100%]**, which is consistent with a judge
that is wrong a quarter of the time. And it was model-vs-model agreement, not
agreement with a human label, so it measured consensus rather than correctness.

THE FREE EXPERIMENT NOBODY RAN. Every ablation run scores a `MUTE` arm: the same
fixed string — `SAFE_FALLBACK` — against the same 89 questions. Three runs are on
disk. That is a **test-retest reliability study already paid for**, sitting in
`evals/results/`, and it says the judge disagrees with itself on roughly a fifth
of identical inputs.

WHAT THAT DOES TO THE NUMBERS. In an 89-case paired comparison you expect ~20
discordant pairs from judge noise alone. Spike 1's published responsiveness
effect is 27 discordant pairs over 178. The effect is not comfortably larger
than the instrument's error, and this file exists so that is stated in a command
rather than discovered by a reviewer.
"""

from __future__ import annotations

import argparse
import collections
import json
from itertools import combinations
from pathlib import Path

from evals.stats import mcnemar, wilson

RESULTS = Path(__file__).parent / "results"


def _mute_runs() -> dict[str, dict[str, bool]]:
    """question -> responsive, per recorded run, for the MUTE arm only."""
    out: dict[str, dict[str, bool]] = {}
    for f in sorted(RESULTS.glob("spike1_*.json")):
        rows = json.loads(f.read_text())["rows"]
        mute = {r["question"]: r["responsive"] for r in rows
                if r["arm"] == "MUTE" and r["responsive"] is not None}
        if mute:
            out[f.stem.replace("spike1_", "")] = mute
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="re-judge the same string N times against a live key")
    ap.add_argument("-n", type=int, default=40)
    a = ap.parse_args()

    print("=" * 78)
    print("SUITE F — the responsiveness judge, measured as an instrument")
    print("=" * 78)

    runs = _mute_runs()
    if len(runs) < 2:
        print("\n   need >=2 recorded ablation runs with a MUTE arm; found "
              f"{len(runs)}\n")
        return 1

    print(f"\n   The MUTE arm sends ONE fixed string against the same questions")
    print(f"   in every run, so repeated runs are a test-retest study.\n")
    for name, m in runs.items():
        k = sum(m.values())
        lo, hi = wilson(k, len(m))
        print(f"      {name[:38]:<40}{k:>3}/{len(m):<4} responsive "
              f"[{lo:.0%}-{hi:.0%}]")

    # --- pairwise disagreement on identical inputs -----------------------
    print(f"\n   pairwise disagreement on IDENTICAL input")
    rates = []
    for (n1, m1), (n2, m2) in combinations(runs.items(), 2):
        shared = set(m1) & set(m2)
        d = sum(1 for q in shared if m1[q] != m2[q])
        rates.append(d / len(shared))
        r = mcnemar(sum(1 for q in shared if m1[q] and not m2[q]),
                    sum(1 for q in shared if m2[q] and not m1[q]))
        print(f"      {n1[:20]:<22} vs {n2[:20]:<22}{d:>4}/{len(shared):<4}"
              f"{d/len(shared):>7.1%}   {r.line()}")
    q = sum(rates) / len(rates)
    print(f"\n      mean pairwise disagreement                     {q:>7.1%}")

    # --- unanimity -------------------------------------------------------
    tally: dict[str, list[bool]] = collections.defaultdict(list)
    for m in runs.values():
        for k_, v in m.items():
            tally[k_].append(v)
    full = {k_: v for k_, v in tally.items() if len(v) == len(runs)}
    split = sum(1 for v in full.values() if len(set(v)) > 1)
    print(f"      questions not unanimous across {len(runs)} runs         "
          f"{split:>4}/{len(full):<4}{split/len(full):>7.1%}")

    # --- what that implies for a paired comparison -----------------------
    print(f"\n   WHAT THIS COSTS THE ABLATION")
    n = len(full)
    print(f"      In an {n}-case paired comparison, judge noise alone is")
    print(f"      expected to produce ~{round(q * n)} discordant pairs.")
    print(f"      Spike 1's published responsiveness effect is 27 discordant")
    print(f"      pairs over 178 — the same order as the instrument's error.")
    print(f"\n      A responsiveness difference smaller than ~{round(q * n)} pairs is")
    print(f"      not distinguishable from the judge changing its mind.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
