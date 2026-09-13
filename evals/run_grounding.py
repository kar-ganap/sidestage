"""Suite C — grounding and abstention.

    uv run python -m evals.run_grounding
    uv run python -m evals.run_grounding --verbose

TWO METRICS, AND THE SECOND IS THE ONE THAT MATTERS (D-26).

**Resolution accuracy** — when a surface form names exactly one thing we stock,
do we find it? This is the ordinary fuzzy-matching question.

**Abstention correctness** — did it ask for clarification *exactly* when it
should have? Two-sided on purpose, because the two errors are not alike:

  - **a confident guess** on a genuinely ambiguous reference is the dangerous
    one. Two Mews are seeded in `catalog.json` specifically so the correct answer
    to "the mew one" is a question. Picking one silently grounds the whole reply
    in the wrong card, and everything downstream — evidence, claims,
    verification — is then correct about something nobody asked about.
  - **an unnecessary question** is the annoying one. Asking "which Charizard?"
    when the viewer already said "champions path zard" wastes the only thing the
    seller is short of, and a copilot that asks twice gets closed.

A suite that scored only the first would be satisfied by a resolver that asks
about everything. Both are reported, always.

WHERE THE CASES COME FROM. All seventeen failure modes in
`docs/research/observation-2026-09-12.md` §C, drawn from twenty minutes of real
chat: misspellings (`dragonight`, `rakwaza`), nicknames (`Zard`), descriptors
that are not attributes (`big boy gengar`), descriptions instead of names
(`shining dragon`), scrambled word order (`gengar fire red`), sets named as
cards, deixis (`the mew one`), plurals (`psyducks`).

**Expectations were written before the resolver was run against them**, from
what the catalogue contains rather than from observed output. Fitting the
expectations to the behaviour would make this suite a description instead of a
test.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.catalog import get_catalog
from app.entities import get_resolver

DATA = Path(__file__).parent / "data"


def load() -> list[dict]:
    rows = [json.loads(l) for l in
            (DATA / "grounding.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    return [r for r in rows if "_meta" not in r]


def observed(res) -> str:
    """Collapse a Resolution into the three outcomes the suite scores."""
    if res.needs_clarification:
        return "abstain"
    if res.items:
        return "item"
    return "none"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    rows = load()
    r = get_resolver()
    cat = get_catalog()

    print("=" * 80)
    print("SUITE C — grounding and abstention, on surfaces observed in real chat")
    print("=" * 80)
    print(f"\n   {len(rows)} cases over {len(cat.items)} catalogue items\n")

    results = []
    for row in rows:
        res = r.resolve(row["text"])
        got = observed(res)
        names = [i.name for i in res.items]
        ok = got == row["expect"]
        if ok and row["expect"] == "item":
            ok = names[:1] == [row["item"]]
        results.append((row, got, names, ok))

    # --- the two headline metrics ---------------------------------------
    for bucket, label in (("item", "resolution"), ("abstain", "abstention"),
                          ("none", "correct silence")):
        sub = [x for x in results if x[0]["expect"] == bucket]
        hit = sum(1 for x in sub if x[3])
        print(f"   {label:<18}{hit:>3}/{len(sub):<4}{hit/len(sub):>7.0%}")

    # Abstention is two-sided, and the sides are not symmetric in cost.
    guessed = [x for x in results if x[0]["expect"] == "abstain" and x[1] == "item"]
    over = [x for x in results if x[0]["expect"] == "item" and x[1] == "abstain"]
    wrong_item = [x for x in results
                  if x[0]["expect"] == "item" and x[1] == "item" and not x[3]]
    missed = [x for x in results if x[0]["expect"] == "item" and x[1] == "none"]
    ghost = [x for x in results if x[0]["expect"] == "none" and x[1] != "none"]

    print(f"\n   {'CONFIDENT GUESS on an ambiguous reference':<48}{len(guessed):>3}"
          f"   <- the dangerous error")
    print(f"   {'asked when the reference was specific enough':<48}{len(over):>3}"
          f"   <- the annoying error")
    print(f"   {'resolved to the WRONG item':<48}{len(wrong_item):>3}")
    print(f"   {'failed to find something we stock':<48}{len(missed):>3}")
    print(f"   {'matched something we do not stock':<48}{len(ghost):>3}")

    bad = [x for x in results if not x[3]]
    if bad:
        print(f"\n   {len(bad)} failing:")
        for row, got, names, _ in bad:
            want = row.get("item") or row["expect"]
            print(f"      {row['text']:<26}[{row['mode']:<34}] "
                  f"want {want!r:<32} got {got}:{names[:2]}")
            if row.get("note"):
                print(f"         {row['note']}")

    if a.verbose:
        print("\n   all cases:")
        for row, got, names, ok in results:
            print(f"      {'  ' if ok else 'X '}{row['text']:<26}{got:<9}{names[:2]}")

    print(f"\n   by failure mode:")
    modes = Counter(row["mode"] for row, _, _, ok in results if not ok)
    if not modes:
        print("      (none failing)")
    for m, n in modes.most_common():
        print(f"      {m:<40}{n}")
    print()
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
