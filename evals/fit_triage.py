"""Fit the stage-1 scorer. Writes `data/triage_weights.json`.

    uv run python -m evals.fit_triage

WHAT IS FIT AND WHAT IS NOT. The *features* are hand-designed, one per failure
mode observed in real chat — that part is domain work and it is where the
thinking is. Only the fifteen weights are fit. This is deliberate: a model whose
inputs a person chose and whose outputs a person can read is defensible under
questioning in a way that "the embedding said so" is not.

THE SPLIT.

    train   triage_train.jsonl        352  synthetic
            triage_extra_batch0.jsonl  83  real, segment before
            triage_extra_batch2.jsonl 241  real, segment after
    ----------------------------------------------------
    test    triage_test.jsonl         161  real, segment middle — NEVER fit on

The honest limitation, stated here because it is easy to forget once there is a
number: the real training segments are **the same show and the same seller** as
the test segment. So this measures generalisation across time within one show,
not across shows. It is a genuine weakness and it belongs in the TDD.

WHY NOT sklearn. Twenty lines of gradient descent has no dependency, runs in
milliseconds on 609 rows, and — the actual reason — can be explained line by line
when someone asks how the model was trained. Importing a fit from a library
answers that question with a shrug.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app.config import DATA_DIR
from app.entities import get_resolver
from app.triage import FEATURE_NAMES, WEIGHTS_PATH, features

DATA = Path(__file__).parent / "data"

TRAIN = ["triage_train", "triage_extra_batch0", "triage_extra_batch2"]
TEST = "triage_test"


def _raw(name: str) -> list[dict]:
    rows = [json.loads(line) for line in
            (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r for r in rows if "_meta" not in r]


def _test_texts() -> set[str]:
    return {r["text"] for r in _raw(TEST)}


def load(name: str, *, drop_test_overlap: bool = True) -> list[dict]:
    """Load a corpus, refusing to hand back anything that is in the test set.

    **Enforced here rather than trusted anywhere (B-36).** The previous version
    filtered only `_meta`, and three separate leaks walked straight through it:

      - `triage_extra_batch0.jsonl`'s own `_meta` names 8 rows that "duplicate
        held-out test rows and must be dropped before the two files are
        concatenated". Nothing dropped them.
      - `triage_train.jsonl` is tagged `"source": "synthetic"` on all 352 rows
        and contains **42 verbatim held-out test messages** — including
        `lugia next!`, `320 for gare plz` and `You got any psyducks`, the exact
        messages the PRD prints as proof the incumbent misses high intent.
      - Between them, 35% of distinct test texts and **56% of test positives**
        were in the training matrix.

    A provenance note in a data file is a comment. This is a filter: the test
    set cannot enter a training load, whatever any `source` field claims.
    """
    rows = _raw(name)
    if not drop_test_overlap or name == TEST:
        return rows
    bad = _test_texts()
    return [r for r in rows if r["text"] not in bad]


def design_matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    res = get_resolver()
    X = np.array([[features(r["text"], res)[k] for k in FEATURE_NAMES] for r in rows])
    y = np.array([1.0 if r.get("seller_directed") else 0.0 for r in rows])
    return X, y


def fit(X: np.ndarray, y: np.ndarray, *, epochs: int = 4000, lr: float = 0.3,
        l2: float = 0.01, pos_weight: float | None = None) -> tuple[np.ndarray, float]:
    """Logistic regression by full-batch gradient descent.

    `pos_weight` upweights the positive class. At a 12% base rate an unweighted
    fit can reach 88% accuracy by predicting "noise" for everything, which is
    exactly the degenerate solution D-26 warns about when it says never to report
    accuracy. Weighting the positives to parity makes the loss care about recall.

    L2 shrinks the weights toward zero, which matters here because several
    features are correlated (`wh_word` and `question_mark` co-occur constantly)
    and an unregularised fit hands them large, unstable, opposite coefficients
    that read as nonsense when you try to explain the model.
    """
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    if pos_weight is None:
        pos_weight = float((y == 0).sum() / max(1, (y == 1).sum()))
    sample_w = np.where(y == 1, pos_weight, 1.0)
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w + b, -60, 60)))
        err = (p - y) * sample_w
        w -= lr * ((X.T @ err) / n + l2 * w)
        b -= lr * (err.mean())
    return w, b


def cv_threshold(X: np.ndarray, y: np.ndarray, *, folds: int = 5, seed: int = 0,
                 **fit_kw) -> tuple[float, float]:
    """Pick the operating point by k-fold cross-validation. Returns (thr, mean F1).

    This exists because of a mistake worth keeping. Adding a feature raised the
    train-fit F1 and *lowered* the held-out score, and the obvious next move —
    compare the two models on the test set and keep the better one — is model
    selection on the test set. It spends the held-out segment to answer a
    question the training data can answer on its own.

    So every choice that is not a weight (which features, which threshold) is
    made here, on folds of the training data, and the held-out segment is read
    once at the end. Cross-validation also gives a *spread* across folds, which
    is the only honest way to tell "better" from "luckier" at this n.
    """
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(y))
    groups = np.array_split(idx, folds)
    grid = [t / 100 for t in range(5, 96)]
    scores = np.zeros(len(grid))
    for k in range(folds):
        hold = groups[k]
        keep = np.concatenate([g for j, g in enumerate(groups) if j != k])
        w, b = fit(X[keep], y[keep], **fit_kw)
        p = 1.0 / (1.0 + np.exp(-np.clip(X[hold] @ w + b, -60, 60)))
        for i, t in enumerate(grid):
            scores[i] += prf(y[hold], (p >= t).astype(float))[2]
    scores /= folds
    best = int(scores.argmax())
    return grid[best], float(scores[best])


def prf(y: np.ndarray, pred: np.ndarray) -> tuple[float, float, float]:
    tp = float(((pred == 1) & (y == 1)).sum())
    fp = float(((pred == 1) & (y == 0)).sum())
    fn = float(((pred == 0) & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=4000)
    ap.add_argument("--l2", type=float, default=0.01)
    ap.add_argument("--capacity", type=float, default=3.0,
                    help="items/min the operator can absorb; sets the operating point")
    ap.add_argument("--dry-run", action="store_true", help="do not write weights")
    a = ap.parse_args()

    rows = [r for name in TRAIN for r in load(name)]
    X, y = design_matrix(rows)
    print(f"train: {len(rows)} rows, {int(y.sum())} positive ({y.mean():.1%})")

    w, b = fit(X, y, epochs=a.epochs, l2=a.l2)

    print("\nfitted weights — sign is the claim, magnitude is the strength")
    print(f"   {'feature':<18}{'weight':>9}")
    for name, wi in sorted(zip(FEATURE_NAMES, w), key=lambda kv: -abs(kv[1])):
        print(f"   {name:<18}{wi:>9.3f}")
    print(f"   {'(bias)':<18}{b:>9.3f}")

    # --- the operating point, chosen HERE and not in the eval ---------------
    #
    # This has to happen on train data. Picking the threshold after looking at a
    # sweep over the test set is tuning on the test set — the score that comes
    # back is then the best of nineteen tries, not an estimate of anything. The
    # whole point of holding batch1 out is lost in one line, and it is an easy
    # line to write by accident.
    # Weights are fit on everything; the THRESHOLD is calibrated on the real
    # segments only. The two need different data and it took a regression to
    # see it. Weights benefit from volume and the synthetic examples are
    # structurally similar, so they help. An operating point does not — it
    # depends on the class balance and the phrasing mix of live traffic, and
    # 352 synthetic rows outvote 324 real ones in a pooled sweep. Calibrating
    # against that picks a cutoff for a distribution that does not exist.
    Xr, yr = design_matrix([r for n in TRAIN if n != "triage_train" for r in load(n)])
    thr, cvf1 = cv_threshold(Xr, yr, epochs=a.epochs, l2=a.l2)
    p_cal = 1.0 / (1.0 + np.exp(-(Xr @ w + b)))
    pr, rc, f1 = prf(yr, (p_cal >= thr).astype(float))
    print(f"\noperating point (5-fold CV on the REAL train segments, n={len(yr)})"
          f"  threshold={thr:.2f}  CV F1={cvf1:.1%}")
    print(f"   refit on all of it at that threshold:  P={pr:.1%} R={rc:.1%} F1={f1:.1%}")

    # Why F1 and not something recall-weighted, stated so it can be argued with:
    # over-surfacing costs operator attention, under-surfacing loses a buyer,
    # and at the observed 0.15 msg/s neither is scarce enough to justify
    # asymmetric weighting. If a real show ran hot, the right move would be to
    # re-derive this against queue capacity rather than to keep F1.
    # Is that threshold a real optimum or one fold's luck? Re-run CV under
    # different shuffles and look at the spread. A cutoff that moves a lot
    # between seeds is a cutoff that will move again on new data.
    seeds = [cv_threshold(Xr, yr, seed=s, epochs=a.epochs, l2=a.l2) for s in range(5)]
    ts = [t for t, _ in seeds]
    fs = [f for _, f in seeds]
    print(f"   across 5 shuffles: threshold {min(ts):.2f}-{max(ts):.2f}"
          f"   CV F1 {sum(fs)/len(fs):.1%} +/- "
          f"{(sum((x - sum(fs)/len(fs))**2 for x in fs)/len(fs))**0.5:.1%}")

    # --- but F1 is the wrong objective, and the pace says so ----------------
    #
    # F1 weights a missed buyer and a wasted glance equally. They are not equal
    # and the observed show says by how much: chat ran at 0.15 msg/s, so even a
    # loose threshold puts only a couple of items a minute in front of the
    # seller, while a dropped question is a lot that closes unanswered.
    #
    # So the operating point is the LOWEST threshold whose surfaced rate stays
    # inside what an operator can actually absorb — recall bought with the slack
    # the pace is handing us, rather than an F1 optimum that assumes symmetric
    # costs nobody has. Same move as the latency budget: derive it from the
    # observation instead of taking a convention.
    #
    # Capacity: lots run 8-15 s, so the seller gets roughly 4-6 glances a
    # minute, and triage is secondary to actually selling. Three items a minute
    # is about one per lot — deliberately conservative, and stated so it can be
    # argued with rather than discovered in the code.
    cap = a.capacity
    p_cal_all = 1.0 / (1.0 + np.exp(-np.clip(Xr @ w + b, -60, 60)))
    per_min = lambda t: float((p_cal_all >= t).mean()) * 0.15 * 60
    feasible = [t / 100 for t in range(5, 96) if per_min(t / 100) <= cap]
    thr_cap = min(feasible) if feasible else thr
    pr_c, rc_c, f1_c = prf(yr, (p_cal_all >= thr_cap).astype(float))
    print(f"\noperating point (queue capacity <= {cap:.0f} items/min at the observed pace)"
          f"  threshold={thr_cap:.2f}")
    print(f"   on the real train segments:  P={pr_c:.1%} R={rc_c:.1%} F1={f1_c:.1%}"
          f"   surfacing {per_min(thr_cap):.1f}/min")
    print(f"   -> shipping this one; the F1 optimum ({thr:.2f}) is reported for"
          f" comparison only")
    thr = thr_cap

    if a.dry_run:
        print("\n--dry-run: not written")
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps({
        "weights": {k: round(float(v), 4) for k, v in zip(FEATURE_NAMES, w)},
        "bias": round(float(b), 4),
        "threshold": round(float(thr), 3),
        "fitted_on": datetime.now(UTC).strftime("%Y-%m-%d"),
        "n_train": len(rows),
        "train_files": TRAIN,
        "note": ("Fit by evals/fit_triage.py. Weights AND threshold are chosen on "
                 "train only; triage_test.jsonl is never fit or tuned against."),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {WEIGHTS_PATH.relative_to(WEIGHTS_PATH.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
