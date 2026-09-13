"""Latency and cost, as distributions rather than medians.

    uv run python -m evals.bench                  # everything
    uv run python -m evals.bench --paths triage   # free paths only
    uv run python -m evals.bench --n-draft 40

WHY THIS EXISTS. Every latency figure quoted in the repo so far is a median of
four to eight probes. Medians are the wrong statistic for this system, and B-16
is the proof: the repair round fires on 6.2% of benign traffic and adds ~7.8 s
when it does. At p50 it is invisible. At p95 it is the whole story.

So this reports **p50 / p95 / p99**, per stage, and the decision log's
"provisional targets, to be replaced by measured p50/p95/p99" stops being a
promise.

WARM-UP IS HANDLED ASYMMETRICALLY, AND THE ASYMMETRY IS THE POINT.

**Deterministic paths are warmed first.** Their cold cost is building the
catalog vocabulary once at process start — it happens before the first request
and never again, so folding it into a per-message p99 describes a request that
does not exist. Leaving it in reported 92 ms against a 50 ms budget; the real
figure is 43 ms.

**Model paths are not warmed.** A cold prompt cache is a recurring state — every
new conversation, every deploy, every gap longer than the TTL — and a reviewer
hitting the deployed app will meet it. Discarding it would flatter exactly the
number D-19's economics rest on. The cache-hit rate is printed so the reader can
see how warm the sample was.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.catalog import get_catalog
from app.entities import get_resolver
from app.evidence import assemble
from app.llm import RATES, FixtureMissing, ReplayClient, get_client
from app.models import Intent
from app.pipeline import draft_reply
from evals.record_fixtures import DEMO
from app.triage import TriageCascade, features, prefilter
from app.verify import verify

DATA = Path(__file__).parent / "data"


@dataclass
class Samples:
    name: str
    ms: list[float] = field(default_factory=list)
    cost: float = 0.0
    cache_hits: int = 0
    calls: int = 0
    extra: str = ""

    def pct(self, q: float) -> float:
        """Nearest-rank percentile.

        Not interpolated, and not `statistics.quantiles`, because at n=20 an
        interpolated p99 is a number no observation supports. Nearest-rank
        returns a latency that actually happened.
        """
        if not self.ms:
            return 0.0
        xs = sorted(self.ms)
        k = max(0, min(len(xs) - 1, int(round(q * len(xs) + 0.5)) - 1))
        return xs[k]

    def line(self, budget: int | None) -> str:
        p95 = self.pct(0.95)
        verdict = "" if budget is None else ("  OK" if p95 <= budget else "  OVER")
        b = f"{budget:>7}" if budget is not None else "      -"
        return (f"   {self.name:<28}{len(self.ms):>5}"
                f"{self.pct(0.50):>9.1f}{p95:>9.1f}{self.pct(0.99):>9.1f}"
                f"{statistics.mean(self.ms):>9.1f}{b}{verdict}")


def load_messages() -> list[str]:
    rows = [json.loads(line) for line in
            (DATA / "triage_test.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r["text"] for r in rows if "_meta" not in r]


# --- deterministic paths ---------------------------------------------------


def bench_resolve(texts: list[str], reps: int) -> Samples:
    s, res = Samples("entity resolve"), get_resolver()
    for t in texts:                      # warm: vocabulary build is once-per-process
        res.resolve(t)
    for _ in range(reps):
        for t in texts:
            t0 = time.perf_counter()
            res.resolve(t)
            s.ms.append((time.perf_counter() - t0) * 1000)
    return s


def bench_features(texts: list[str], reps: int) -> Samples:
    s, res = Samples("triage stage 1 (gate)"), get_resolver()
    for t in texts:
        features(t, res)
    for _ in range(reps):
        for t in texts:
            t0 = time.perf_counter()
            if prefilter(t) is None:
                features(t, res)
            s.ms.append((time.perf_counter() - t0) * 1000)
    return s


def bench_assemble(reps: int) -> Samples:
    s = Samples("evidence assemble")
    cat, res = get_catalog(), get_resolver()
    lot = cat.lots.get("lot_001")
    r = res.resolve("is that 1st edition?")
    for _ in range(reps):
        t0 = time.perf_counter()
        assemble(intent=Intent.ATTRIBUTE_Q, resolution=r, catalog=cat, lot=lot)
        s.ms.append((time.perf_counter() - t0) * 1000)
    return s


def bench_verify(reps: int) -> tuple[Samples, Samples]:
    """The number the whole claim contract rests on.

    D-09's argument is that assembling evidence *before* generation turns
    verification into dict lookups. If this is not sub-millisecond, that
    argument is decorative.

    **Measured over the real recorded corpus, not one synthetic draft** (B-80).
    The old version timed a single two-claim reply, which said nothing about a
    three-sentence one — and a claim of "0.2 ms" that holds only for the
    shortest draft in the set is not a claim about the system.

    **CPU time as well as wall time.** Wall-clock tails on a shared machine are
    scheduling noise rather than work: one run reported a warm p99 *above* its
    cold p99, which is incoherent and was the tell. `process_time()` counts only
    CPU actually spent here, which answers "how much work is this"; wall time is
    what an operator experiences, tenants and all. Both are reported because
    they answer different questions and only one of them is about the code.
    """
    cpu = Samples("verify — CPU (the work)")
    wall = Samples("verify — wall (this machine)")
    cat, res = get_catalog(), get_resolver()
    client = ReplayClient(strict=True)
    drafts = []
    for msg, intent, lot_id in DEMO:
        try:
            r = draft_reply(msg, intent=intent,
                            lot=cat.lots.get(lot_id) if lot_id else None,
                            catalog=cat, resolver=res, client=client,
                            max_repairs=0)
        except FixtureMissing:
            continue
        drafts.append(r)
    if not drafts:                       # no fixtures recorded yet
        return cpu, wall
    per = max(1, reps // len(drafts))
    for r in drafts:
        for i in range(per + 3):
            t0, c0 = time.perf_counter(), time.process_time()
            verify(r.draft, r.evidence, catalog=cat)
            if i < 3:
                continue                 # warm the regexes for this draft
            wall.ms.append((time.perf_counter() - t0) * 1000)
            cpu.ms.append((time.process_time() - c0) * 1000)
    return cpu, wall


# --- model paths -----------------------------------------------------------

TRIAGE_PROBES = ["Do u have the mew one", "is that 1st edition?", "wtf lmaooo",
                 "Any team rocket holos", "320 for gare plz"]
DRAFT_PROBES = [("is that 1st edition?", Intent.ATTRIBUTE_Q, "lot_001"),
                ("what grade is it", Intent.GRADE_CONDITION_Q, "lot_001"),
                ("what's this worth?", Intent.PRICE_VALUE_Q, "lot_001"),
                ("Any team rocket holos", Intent.AVAILABILITY_Q, None)]


def bench_triage_escalated(n: int) -> Samples:
    s = Samples("triage stage 2 (escalated)")
    casc = TriageCascade()
    llm = get_client()
    for i in range(n):
        msg = TRIAGE_PROBES[i % len(TRIAGE_PROBES)]
        t0 = time.perf_counter()
        r = llm.classify(message=msg)
        s.ms.append((time.perf_counter() - t0) * 1000)
        s.calls += 1
        s.cache_hits += int(r.cache_hit)
        s.cost += r.cost_usd(RATES.get(r.model))
    _ = casc
    return s


def bench_draft(n: int) -> tuple[Samples, Samples, Samples]:
    """Three distributions from one set of runs, because they answer different
    questions: when the operator can read, when they can send, and how much of
    the tail is the repair round (D-35, B-16)."""
    ttft, send, repaired = (Samples("draft: to first token"),
                            Samples("draft: to sendable"),
                            Samples("draft: to sendable (repaired only)"))
    cat, res, llm = get_catalog(), get_resolver(), get_client()
    for i in range(n):
        msg, intent, lot_id = DRAFT_PROBES[i % len(DRAFT_PROBES)]
        r = draft_reply(msg, intent=intent, lot=cat.lots.get(lot_id) if lot_id else None,
                        catalog=cat, resolver=res, client=llm)
        if r.ttft_ms:
            ttft.ms.append(float(r.ttft_ms))
        send.ms.append(float(r.total_ms))
        if r.attempts > 1:
            repaired.ms.append(float(r.total_ms))
    return ttft, send, repaired


def _save(rows: list[tuple[Samples, int | None]]) -> None:
    """Write the distributions so `tools/check_docs.py` can verify a quoted
    number without re-running anything. A doc check nobody runs is not a check.
    """
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    (out / "bench.json").write_text(json.dumps({
        s.name: {"n": len(s.ms), "p50": round(s.pct(0.50), 3),
                 "p95": round(s.pct(0.95), 3), "p99": round(s.pct(0.99), 3)}
        for s, _ in rows if s.ms}, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", choices=["all", "triage", "free"], default="all")
    ap.add_argument("--n-draft", type=int, default=16)
    ap.add_argument("--n-triage", type=int, default=20)
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()

    texts = load_messages()
    print("=" * 84)
    print("BENCH — latency distributions, replacing the provisional targets in D-35")
    print("=" * 84)
    print(f"\n   {'path':<28}{'n':>5}{'p50':>9}{'p95':>9}{'p99':>9}{'mean':>9}"
          f"{'budget':>7}")

    # Resolve cost scales with token count — 1 token is ~0 ms, a 19-token
    # message is ~55 ms, because every unmatched token triggers a fuzzy scan
    # over the catalog. It is the dominant term in stage 1 and the reason the
    # gate is milliseconds rather than the microseconds D-15 predicted.
    rows: list[tuple[Samples, int | None]] = [
            (bench_resolve(texts, a.reps), None),
            (bench_features(texts, a.reps), 50),
            (bench_assemble(200 * a.reps), None)]
    rows += [(x, None) for x in bench_verify(200 * a.reps)]
    for s, b in rows:
        print(s.line(b))

    _save(rows)
    if a.paths == "free":
        print("\n   (--paths free: model paths skipped)\n")
        return 0

    esc = bench_triage_escalated(a.n_triage)
    print(esc.line(600))
    print(f"      cache hits {esc.cache_hits}/{esc.calls}"
          f"   ${esc.cost/max(1,esc.calls):.5f}/call   ${esc.cost:.4f} total")

    if a.paths == "triage":
        print()
        return 0

    ttft, send, rep = bench_draft(a.n_draft)
    print(ttft.line(1500))
    print(send.line(3000))
    if rep.ms:
        print(rep.line(None))
        print(f"      repair fired on {len(rep.ms)}/{len(send.ms)} "
              f"({len(rep.ms)/len(send.ms):.0%}) — this is the tail (B-16)")
    else:
        print("   draft: no repair fired in this sample")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
