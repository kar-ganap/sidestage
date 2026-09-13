"""Spike 1's result, across every recorded run — never one of them.

    uv run python -m evals.report_spike1

WHY THIS EXISTS (B-100). The published ablation table spliced two runs: the S0
row from one, the S1/S2 rows from another taken seventeen minutes later. In the
run S0 actually came from, S2 scored **below** S1 — verification costing safety
— and the table reported +2.3 the other way.

Nobody chose that dishonestly; there was no aggregation step, so writing the
table meant copying numbers by hand from two logs, and the hand copied the
favourable pair. The repo already applies the correct discipline elsewhere —
B2's over-block rate is quoted as "7.8% in four of five runs, 10.4% in one", and
A2's precision is a three-run mean — and did not apply it to the one experiment
whose effect turned out to be smaller than its run-to-run variance.

So: no hand-copying. This reads every `spike1_*.json` in `results/`, groups by
drafting model, and prints the spread. A single run is labelled as a sample
rather than a rate.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

from evals.stats import Spread, mcnemar, wilson

RESULTS = Path(__file__).parent / "results"


def _safe(r: dict) -> bool | None:
    if r["blocked"]:
        return True
    return None if r["asserts"] is None else not r["asserts"]


def _rate(rows: list[dict], fn) -> float | None:
    vals = [v for v in (fn(r) for r in rows) if v is not None]
    return sum(vals) / len(vals) if vals else None


def main() -> int:
    # B-113. Group by (model, arm-set, case count), not by model alone. The
    # filename already encodes the arm set — `run_spike1` namespaces it
    # precisely so runs cannot clobber each other — and pooling by model undid
    # that: a two-case smoke run dropped into `results/` silently widened every
    # published range to `[0.0% - 97.8%]`.
    runs: dict[tuple, list[Path]] = collections.defaultdict(list)
    for f in sorted(RESULTS.glob("spike1_*.json")):
        d = json.loads(f.read_text())
        cases = len({r["case_id"] for r in d["rows"]})
        runs[(d["draft_model"], tuple(d.get("arms", [])), cases)].append(f)

    print("=" * 78)
    print("SPIKE 1 — every recorded run, and the spread")
    print("=" * 78)

    for (model, arms_key, cases), files in sorted(runs.items()):
        per_arm: dict[str, dict[str, list[float]]] = collections.defaultdict(
            lambda: collections.defaultdict(list))
        paired: dict[str, list[int]] = collections.defaultdict(list)

        for f in files:
            rows = json.loads(f.read_text())["rows"]
            by_arm = collections.defaultdict(list)
            for r in rows:
                by_arm[r["arm"]].append(r)
            for arm, sub in by_arm.items():
                for axis, fn in (("safe", _safe),
                                 ("responsive", lambda r: r["responsive"]),
                                 ("blocked", lambda r: r["blocked"])):
                    v = _rate(sub, fn)
                    if v is not None:
                        per_arm[arm][axis].append(v)
            # paired discordance within THIS run, then pooled across runs
            idx = {r["case_id"]: r for r in by_arm.get("S1", [])}
            for r2 in by_arm.get("S2", []):
                r1 = idx.get(r2["case_id"])
                if not r1:
                    continue
                a, b = _safe(r1), _safe(r2)
                if a is not None and b is not None:
                    paired["safe_b"].append(1 if (a and not b) else 0)
                    paired["safe_c"].append(1 if (b and not a) else 0)
                x, y = r1["responsive"], r2["responsive"]
                if x is not None and y is not None:
                    paired["resp_b"].append(1 if (x and not y) else 0)
                    paired["resp_c"].append(1 if (y and not x) else 0)

        n_runs = len(files)
        print(f"\n  {model}   {n_runs} run{'s' if n_runs != 1 else ''}"
              f"   arms {'/'.join(arms_key) or '?'}   {cases} cases\n")
        print(f"      {'arm':<14}{'SAFE':>24}{'RESPONSIVE':>26}")
        for arm in ("S0", "S1", "S2", "MUTE"):
            if arm not in per_arm:
                continue
            s = Spread(per_arm[arm]["safe"]) if per_arm[arm]["safe"] else None
            r = Spread(per_arm[arm]["responsive"])
            # B-113: an arm present in fewer runs than the group must say so,
            # rather than printing a one-element "range" that looks measured.
            def cell(sp: Spread | None) -> str:
                if sp is None:
                    return "—"
                if len(sp.values) == 1:
                    return f"{sp.mid:.1%}  (1 run)"
                return f"{sp.mid:.1%} [{sp.lo:.1%}-{sp.hi:.1%}]"
            print(f"      {arm:<14}{cell(s):>24}{cell(r):>26}")

        # the delta, per run, which is the number the splice hid
        if "S1" in per_arm and "S2" in per_arm:
            s1, s2 = per_arm["S1"]["safe"], per_arm["S2"]["safe"]
            if len(s1) == len(s2) and s1:
                d = [b - a for a, b in zip(s1, s2)]
                print(f"\n      S1 -> S2 safety delta, PER RUN: "
                      f"{', '.join(f'{x:+.1%}' for x in d)}")
                if min(d) < 0 < max(d) or 0 in d:
                    print( "         the sign is not stable across runs — the "
                           "effect is inside the noise")
            r1, r2 = per_arm["S1"]["responsive"], per_arm["S2"]["responsive"]
            if len(r1) == len(r2) and r1:
                d = [b - a for a, b in zip(r1, r2)]
                print(f"      S1 -> S2 responsiveness delta, PER RUN: "
                      f"{', '.join(f'{x:+.1%}' for x in d)}")

        if paired["safe_b"]:
            b, c = sum(paired["safe_b"]), sum(paired["safe_c"])
            print(f"\n      paired over all runs, safety:         "
                  f"{mcnemar(b, c).line()}")
            rb, rc = sum(paired["resp_b"]), sum(paired["resp_c"])
            print(f"      paired over all runs, responsiveness: "
                  f"{mcnemar(rb, rc).line()}")

    print("\n  NOTE. Pooling runs of the same model is pooling repeated measures")
    print("  of the SAME cases, so the pooled p is optimistic: case difficulty")
    print("  is a shared effect and the pairs are not independent. The per-run")
    print("  spread above is the honest summary; the test is a cross-check.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
