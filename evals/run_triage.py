"""Suite A — triage against a measured incumbent. Spike 2's headline.

WHAT "INCUMBENT" MEANS HERE, PRECISELY (B-84). On Whatnot it means the
platform's own highlighter, and the comparison is legitimate because the two
were shown to be the same rule: across 485 observed messages `highlighted` and
`"?" in text` agree 485/485. On eBay Live there is no highlighter to compare
against, so the same arm is a **naive `?` baseline** and is labelled that way.
The distinction matters because "we beat the platform" and "we beat a regex" are
different claims, and only one of them is available on each platform.

    uv run python -m evals.run_triage              # full, with the model arm
    uv run python -m evals.run_triage --no-llm     # free, deterministic arms only

THE OPPONENT IS REAL. `triage_test.jsonl` records, per message, whether
Whatnot's own UI highlighted it. So the incumbent's score is computed from the
same file as ours, on the same rows, rather than asserted.

THE ABLATION IS THE POINT. Three arms, each adding one thing:

    A0  question-mark regex        what the platform does today
    A1  + stage-1 gate             reads intent; no model call, no cost
    A2  + stage-2 classification   the full cascade

A0 -> A1 says whether the *features* earn their place. A1 -> A2 says whether the
model does. If A1 alone beats the incumbent, that is a stronger result than A2
beating it, because it means the win came from reading intent rather than from
spending money — and it is the arm that runs in under a millisecond.

WHY PRECISION IS ALSO REPORTED AS A RATE. A percentage does not tell an operator
anything they can act on. At the observed pace (0.15 msg/s) what matters is how
many items per minute land in a queue they glance at for two seconds between
lots. Same number, in the unit the constraint is actually felt in.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.entities import get_resolver
from app.models import Intent
from app.triage import Model, TriageCascade, features, prefilter

DATA = Path(__file__).parent / "data"

SEGMENTS = [("batch0 · before", "triage_extra_batch0"),
            ("batch1 · TEST",   "triage_test"),
            ("batch2 · after",  "triage_extra_batch2")]
TEST = "triage_test"

OBSERVED_MSGS_PER_SEC = 0.15  # docs/research/chat-analysis-2026-09-12.md


def load(name: str) -> list[dict]:
    rows = [json.loads(line) for line in
            (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r for r in rows if "_meta" not in r]


# --- scoring ---------------------------------------------------------------


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval.

    Used instead of the textbook normal interval because n here is 10-32
    positives per segment, where the normal approximation produces bounds
    outside [0,1] and understates uncertainty at the extremes. The per-segment
    spread is the claim this interval exists to keep honest.
    """
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def score(rows: list[dict], pred: list[bool]) -> dict:
    tp = sum(1 for r, q in zip(rows, pred) if q and r.get("seller_directed"))
    fp = sum(1 for r, q in zip(rows, pred) if q and not r.get("seller_directed"))
    fn = sum(1 for r, q in zip(rows, pred) if not q and r.get("seller_directed"))
    p, r, f1 = prf(tp, fp, fn)
    n_pos, n_pred = tp + fn, tp + fp
    return dict(tp=tp, fp=fp, fn=fn, p=p, r=r, f1=f1, n=len(rows), n_pos=n_pos,
                r_lo=wilson(tp, n_pos)[0], r_hi=wilson(tp, n_pos)[1],
                p_lo=wilson(tp, n_pred)[0], p_hi=wilson(tp, n_pred)[1],
                surfaced=sum(pred),
                per_min=sum(pred) / len(rows) * OBSERVED_MSGS_PER_SEC * 60)


def row(label: str, s: dict) -> str:
    """Intervals on both rates, not just the point estimates.

    A precision of 100% over 21 predictions is not 100% — it is "somewhere above
    ~84%", and printing the bare figure invites a claim the data cannot support.
    Suite A exists to beat a measured opponent honestly; a number without its
    uncertainty is the easiest way to stop doing that.
    """
    return (f"   {label:<26}{s['r']:>6.1%} [{s['r_lo']:.0%}-{s['r_hi']:.0%}]"
            f"{s['p']:>8.1%} [{s['p_lo']:.0%}-{s['p_hi']:.0%}]"
            f"{s['f1']:>8.1%}{s['tp']:>5}{s['fp']:>5}{s['fn']:>5}{s['per_min']:>8.1f}")


# --- the three arms --------------------------------------------------------


def arm_question_mark(rows: list[dict]) -> list[bool]:
    """A bare `?` test — and on Whatnot, demonstrably the incumbent.

    **The equivalence is measured, not assumed:** across all 485 observed
    Whatnot messages, every highlighted row contains `?` and no unhighlighted
    row does. 485/485, zero disagreements. So on that platform this function and
    Whatnot's highlighter are the same rule, and calling its score "the
    incumbent's" is a statement about the platform rather than a flattering
    name for a regex.

    **It is NOT the incumbent anywhere else** (B-84). eBay Live has no visible
    highlight at all — `docs/research/observation-ebaylive-2026-09-13.md` records
    that `highlighted` "cannot be read off" there, so every row in
    `triage_show2.jsonl` carries `highlighted: false` as an ABSENCE MARKER, not
    as an observation that the platform declined to highlight it.

    This function was previously named `arm_incumbent` and its docstring cited
    the Whatnot verification to justify running it on any row set. That made the
    cross-platform line read "the cascade does not beat the incumbent on a new
    platform", when what it actually compares against is a naive `?` baseline on
    a platform where no incumbent exists to compare against. The conclusion
    survives — it was a withdrawal either way — but the reason it was withdrawn
    has to be the true one.
    """
    return ["?" in r["text"] for r in rows]


# Kept as an alias so a reader grepping for the old name lands on the docstring
# above rather than on nothing.
arm_incumbent = arm_question_mark


def arm_gate(rows: list[dict], model: Model, thr: float) -> list[bool]:
    res = get_resolver()
    out = []
    for r in rows:
        if prefilter(r["text"]) is not None:
            out.append(False)
            continue
        out.append(model.score(features(r["text"], res)) >= thr)
    return out


def arm_cascade(rows: list[dict], thr: float, workers: int) -> tuple[list[bool], list[Intent], int]:
    casc = TriageCascade(drop_below=thr)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda r: casc.triage(r["text"]), rows))
    return ([x.surfaced for x in results],
            [x.intent for x in results],
            sum(1 for x in results if x.escalated))


# --- operating point -------------------------------------------------------


def sweep(rows: list[dict], model: Model) -> list[tuple[float, dict]]:
    res = get_resolver()
    scores = [0.0 if prefilter(r["text"]) is not None
              else model.score(features(r["text"], res)) for r in rows]
    out = []
    for thr in [i / 20 for i in range(1, 20)]:
        out.append((thr, score(rows, [s >= thr for s in scores])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip the cascade arm")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=None)
    a = ap.parse_args()

    model = Model.load()
    # Read, not chosen. See Model.threshold — picking it here would be
    # tuning on the held-out segment.
    thr = a.threshold if a.threshold is not None else model.threshold
    rows = load(TEST)

    print("=" * 86)
    print("SUITE A — triage vs. the incumbent, on the held-out segment")
    print("=" * 86)
    print(f"\nmodel fit {model.fitted_on or '(unfitted)'} on {model.n_train} rows · "
          f"test {len(rows)} rows, {sum(1 for r in rows if r.get('seller_directed'))} "
          f"seller-directed · threshold {thr}")

    print(f"\n   {'arm':<26}{'recall (95% CI)':>18}{'precision (95% CI)':>20}"
          f"{'F1':>8}{'tp':>5}{'fp':>5}{'fn':>5}{'/min':>8}")

    inc = score(rows, arm_incumbent(rows))
    print(row("A0  question-mark regex", inc))

    t0 = time.perf_counter()
    gate = score(rows, arm_gate(rows, model, thr))
    gate_ms = (time.perf_counter() - t0) * 1000 / len(rows)
    print(row("A1  + stage-1 gate", gate))

    casc = esc = None
    pred: list[bool] = []
    intents: list = []
    if not a.no_llm:
        pred, intents, esc = arm_cascade(rows, thr, a.workers)
        casc = score(rows, pred)
        print(row("A2  + classification", casc))

    print(f"\n   deltas vs incumbent      recall {gate['r']-inc['r']:+.1%}"
          f"   precision {gate['p']-inc['p']:+.1%}   F1 {gate['f1']-inc['f1']:+.1%}   (A1)")
    if casc:
        print(f"                            recall {casc['r']-inc['r']:+.1%}"
              f"   precision {casc['p']-inc['p']:+.1%}   F1 {casc['f1']-inc['f1']:+.1%}   (A2)")
        print(f"   escalated {esc}/{len(rows)} ({(esc or 0)/len(rows):.0%}) — "
              f"the rest cost nothing")

    # --- what the incumbent misses, and whether we get it -------------------
    # Indexed rather than by value: two viewers do type the same message, and
    # `rows.index(r)` would attribute the second one's outcome to the first.
    inc_pred, gate_pred = arm_incumbent(rows), arm_gate(rows, model, thr)
    missed = [i for i, r in enumerate(rows)
              if r.get("seller_directed") and not inc_pred[i]]
    caught = [i for i in missed if gate_pred[i]]
    print(f"\n   of the {len(missed)} seller-directed messages the regex misses, "
          f"A1 catches {len(caught)} and A2 keeps "
          f"{sum(1 for i in caught if pred[i]) if pred else '-'}:")
    for i in missed:
        mark = "OK " if gate_pred[i] else "   "
        if pred and gate_pred[i] and not pred[i]:
            mark = "vetoed "
        print(f"      {mark:<8}[{rows[i]['intent']:<17}] {rows[i]['text'][:52]}")

    # --- stability, with intervals -----------------------------------------
    print(f"\n   per-segment recall (Wilson 95% — n is 10-32 positives, so these"
          f" are wide on purpose)")
    print(f"      {'segment':<18}{'incumbent':>22}{'A1 gate':>24}")
    for label, name in SEGMENTS:
        sr = load(name)
        i, g = score(sr, arm_incumbent(sr)), score(sr, arm_gate(sr, model, thr))
        print(f"      {label:<18}{i['r']:>9.1%} [{i['r_lo']:.0%}-{i['r_hi']:.0%}]"
              f"{g['r']:>11.1%} [{g['r_lo']:.0%}-{g['r_hi']:.0%}]")
    print("      NOTE batch0/batch2 are TRAIN data for A1 — only batch1 is held out.")

    # --- operating point ----------------------------------------------------
    print(f"\n   threshold sweep on the held-out segment")
    print(f"      {'thr':>5}{'recall':>9}{'prec':>8}{'F1':>8}{'/min':>8}")
    for t, s in sweep(rows, model):
        if t in (0.05, 0.15, 0.25, 0.35, 0.5, 0.65, 0.8, 0.9) or abs(t - thr) < 0.025:
            star = "  <- default" if abs(t - thr) < 1e-9 else ""
            print(f"      {t:>5.2f}{s['r']:>9.1%}{s['p']:>8.1%}{s['f1']:>8.1%}"
                  f"{s['per_min']:>8.1f}{star}")

    print(f"\n   stage-1 latency {gate_ms:.2f} ms/message "
          f"(budget: p95 <= 50 ms deterministic)")

    if casc:
        print(f"\n   intent accuracy on correctly-surfaced messages")
        ok = sum(1 for r, q, i in zip(rows, pred, intents)
                 if q and r.get("seller_directed") and i.value == r["intent"])
        tot = sum(1 for r, q in zip(rows, pred) if q and r.get("seller_directed"))
        print(f"      {ok}/{tot} exact intent match ({ok/tot:.0%})" if tot else "      n/a")
        conf = Counter((r["intent"], i.value) for r, q, i in zip(rows, pred, intents)
                       if q and r.get("seller_directed") and i.value != r["intent"])
        for (truth, got), k in conf.most_common(6):
            print(f"      {truth:<18} -> {got:<18} x{k}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
