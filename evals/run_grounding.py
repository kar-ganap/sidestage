"""Suite C — grounding and abstention.

    uv run python -m evals.run_grounding
    uv run python -m evals.run_grounding --verbose

THREE ARMS. The curated suite scores 100% on all of its axes, which is what an
eval written by whoever wrote the resolver looks like: it contains the surfaces
they thought of. So there is a third arm that **can fail** — the same 36 cases
under realistic chat noise, where the expected answer is unchanged because
"champion's path zard" and "champions path zard" name the same card.

It scores **77%**, and the interesting number is not the rate:

    of the 27 it loses, 0 resolve to the WRONG item and 27 find nothing

**It degrades to silence, not to confident error.** That distinction is the
whole point of D-14: a failure to find costs the buyer an answer, while a wrong
find grounds the entire reply — evidence, claims, verification — in a card
nobody asked about, and every downstream check then passes.

The weakest mode is lost spacing (61%): `mewtwoarmored`, `celebrationsmew`.
Known and deliberately not patched yet — closing it means matching across word
boundaries, which is precisely the change most likely to convert safe silences
into confident errors, and the suite above is what would have to prove it did
not.

TWO CURATED METRICS, AND THE SECOND IS THE ONE THAT MATTERS (D-26).

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


# --- the arm that can actually fail (B-82) ----------------------------------
#
# The curated suite scores 100% on every axis, which is what a suite written by
# the person who wrote the resolver looks like: it contains the surfaces they
# thought of, and a perfect score on those says nothing about the ones they did
# not. An eval that cannot fail is not measuring.
#
# Perturbation is scoreable without new labels, because the EXPECTED ANSWER IS
# UNCHANGED: "champion's path zard" and "champions path zard" name the same
# card, and a viewer typing at auction speed produces both. If accuracy holds
# under noise the 100% is a property of the resolver; if it collapses, the 100%
# was a property of the curation.

def _perturbations(text: str) -> list[tuple[str, str]]:
    """Realistic chat noise. Every one of these was observed in the transcripts:
    dropped apostrophes, dropped vowels, doubled letters, lost spacing, case."""
    import re as _re
    out: list[tuple[str, str]] = []
    if "'" in text or "\u2019" in text:
        out.append(("apostrophe dropped", _re.sub(r"['\u2019]", "", text)))
    out.append(("upper", text.upper()))
    words = text.split()
    if len(words) > 1:
        out.append(("space lost", words[0] + words[1] +
                    ("" if len(words) == 2 else " " + " ".join(words[2:]))))
    longest = max(words, key=len) if words else ""
    if len(longest) > 4:
        i = len(longest) // 2
        out.append(("letter doubled",
                    text.replace(longest, longest[:i] + longest[i] + longest[i:], 1)))
        out.append(("letter dropped",
                    text.replace(longest, longest[:i] + longest[i + 1:], 1)))
        out.append(("transposed",
                    text.replace(longest,
                                 longest[:i] + longest[i + 1] + longest[i] +
                                 longest[i + 2:], 1)))
    return out


def _robustness(rows: list[dict], r) -> None:
    total = hit = dangerous = silent = 0
    worst: dict[str, list[int]] = {}
    examples: list[str] = []
    for row in rows:
        for label, text in _perturbations(row["text"]):
            if text.strip().lower() == row["text"].strip().lower():
                continue
            res = r.resolve(text)
            got = observed(res)
            ok = got == row["expect"]
            if ok and row["expect"] == "item":
                ok = [i.name for i in res.items][:1] == [row["item"]]
            total += 1
            hit += ok
            if not ok:
                wrong_item = (row["expect"] == "item" and got == "item"
                              and [i.name for i in res.items][:1] != [row["item"]])
                guessed_at_ambiguous = row["expect"] == "abstain" and got == "item"
                if wrong_item or guessed_at_ambiguous:
                    dangerous += 1
                else:
                    silent += 1
            w = worst.setdefault(label, [0, 0])
            w[0] += ok
            w[1] += 1
            if not ok and len(examples) < 6:
                examples.append(f"{row['text']!r} -> {text!r}: "
                                f"expected {row['expect']}"
                                f"{'/' + row['item'] if row.get('item') else ''}, "
                                f"got {got}"
                                f"{'/' + res.items[0].name if res.items else ''}")
    if not total:
        return
    print(f"\n   UNDER PERTURBATION — the arm that can fail")
    print(f"      {hit}/{total}{hit / total:>9.0%}   same expected answer, "
          f"realistic chat noise")
    # The RATE matters less than the DIRECTION. Degrading to silence costs the
    # buyer an answer; degrading to the wrong card grounds the entire reply —
    # evidence, claims, verification — in something nobody asked about, and
    # every downstream check then passes. D-14 exists for exactly this.
    print(f"      of the {total - hit} it loses: "
          f"{dangerous} resolved to the WRONG item, {silent} found nothing")
    if dangerous == 0:
        print( "         it degrades to silence, not to confident error")
    for label, (h, n) in sorted(worst.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print(f"         {label:<20}{h:>3}/{n:<4}{h / n:>7.0%}")
    if examples:
        print("      what it loses:")
        for e in examples:
            print(f"         {e}")


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

    _robustness(rows, r)

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
