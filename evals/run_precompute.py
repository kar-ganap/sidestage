"""Does the warm cache earn its place? Measured on the real transcript.

    uv run python -m evals.run_precompute

THREE QUESTIONS, and the last two matter more than the first.

**1. What is the hit rate?** D-36 promised a hit on any question of a known
intent. D-36b corrected the key to `(lot, question)`, which is safe and far
narrower, so the honest number is whatever genuine repeats occur — measured
here against the 161-message transcript rather than estimated.

**2. Does re-verification actually reject a stale hit?** The entire safety
argument rests on it, so it is exercised directly: warm a draft, move the world
underneath it, and confirm the cache refuses to serve.

**3. Would a `(lot, intent)` key have served the wrong answer?** The D-36b
flaw, quantified rather than asserted — how many times would the original design
have handed a viewer an answer to a different question.

Run with fixtures (`SIDESTAGE_LLM_MODE=replay`) for a free deterministic pass,
or live to measure real warm cost.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.catalog import get_catalog
from app.entities import get_resolver
from app.llm import ReplayClient, get_client
from app.models import Intent, Verdict
from app.precompute import CANONICAL, PrecomputeCache
from app.triage import TriageCascade, _shingle

DATA = Path(__file__).parent / "data"


def transcript(name: str = "triage_test") -> list[str]:
    rows = [json.loads(l) for l in
            (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r["text"] for r in rows if "_meta" not in r]


def main() -> int:
    cat, res = get_catalog(), get_resolver()
    llm = get_client()
    print("=" * 76)
    print("PRECOMPUTE — a warm cache of verified drafts (D-36b)")
    print("=" * 76)

    cache = PrecomputeCache(catalog=cat, resolver=res, client=llm)
    t0 = time.perf_counter()
    n = cache.warm()
    warm_s = time.perf_counter() - t0
    lots = cache._upcoming()
    print(f"\n   warmed {n} drafts across {len(lots)} lots "
          f"({len(CANONICAL)} canonical questions each) in {warm_s:.1f}s")
    print(f"   {len(CANONICAL) * len(lots) - n} were blocked and NOT cached — "
          f"serving an instant refusal is worse than useless")

    # --- 1. hit rate on real traffic ------------------------------------
    print("\n   1. hit rate on the real transcript")
    casc = TriageCascade(resolver=res, client=llm)
    lot = cat.live_lot
    asked = served = 0
    for text in transcript():
        r = casc.triage(text)
        if not r.surfaced:
            continue
        asked += 1
        if cache.lookup(text, lot, r.intent):
            served += 1
    print(f"      {served}/{asked} surfaced questions served from cache "
          f"({served/asked:.0%})" if asked else "      no questions surfaced")
    print(f"      D-36 promised a hit on any question of a known intent.")
    print(f"      D-36b's key is (lot, question), so this is genuine repeats only.")

    # --- 2. does re-verification reject a moved world? -------------------
    print("\n   2. does re-verification reject a stale hit?")
    probe = "whats the bid at"
    before = cache.lookup(probe, lot, Intent.PRICE_VALUE_Q)
    print(f"      before moving anything: {'HIT' if before else 'miss'}"
          + (f" in {before[1]}us" if before else ""))
    if lot is not None and before:
        original = lot.current_bid
        lot.current_bid = (original or 0) + 500        # a bid lands
        after = cache.lookup(probe, lot, Intent.PRICE_VALUE_Q)
        print(f"      after a $500 bid lands:  {'HIT' if after else 'REJECTED (stale)'}")
        if after:
            print("      PROBLEM: the cache served a draft built on a stale bid")
        lot.current_bid = original

    # --- 3. what the original (lot, intent) key would have done ----------
    print("\n   3. what a (lot, intent) key would have served")
    wrong = 0
    for text in transcript():
        r = casc.triage(text)
        if not r.surfaced or lot is None:
            continue
        same_intent = [e for e in cache.entries.get(lot.id, []) if e.intent is r.intent]
        if not same_intent:
            continue
        want = _shingle(text)
        # (lot, intent) would have served the first entry regardless of wording.
        e = same_intent[0]
        sim = len(want & e.shingle) / len(want | e.shingle) if want and e.shingle else 0.0
        if sim < cache.similarity:
            wrong += 1
            if wrong <= 3:
                print(f"      asked  {text[:44]!r}")
                print(f"      served {e.question!r}  (similarity {sim:.2f})")
    print(f"      {wrong} questions would have been answered with a cached reply")
    print(f"      about something else — every claim true, the answer wrong (B-13).")

    s = cache.stats
    print(f"\n   cache: {s.lookups} lookups, {s.hits} hits, {s.stale} rejected as stale,"
          f" {s.misses} misses")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
