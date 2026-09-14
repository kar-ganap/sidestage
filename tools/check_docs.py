#!/usr/bin/env python3
"""Every number the docs state must be derivable, and must still be true.

    uv run python tools/check_docs.py

WHY THIS EXISTS. An adversarial review found the PRD claiming 41 decisions
against 44 in the file, 130 fixtures against 199 on disk, a judge described as
unbuilt after it shipped, and the incumbent's recall stated as both 53.6% and
45.9% two paragraphs apart. None of it was dishonest; all of it was a number
hand-copied once and never re-derived. In a document whose whole purpose is to
say what was measured, a stale number is indistinguishable from a made-up one.

HOW IT WORKS. Each fact below has a callable that computes the truth and a list
of places the docs state it. The regex lives HERE, not in the prose, so the
documents stay readable — and a reworded sentence that stops matching is
reported as `claim not found` rather than passing silently, which is the other
half of the guarantee.

WHAT IT DELIBERATELY DOES NOT CHECK. `docs/BUILD-LOG.md` is a log: entries say
what was true on the day they were written ("154 tests", "46 tests on
unreachable code") and retro-editing them would destroy the only record of what
changed when. A log is not a claim about the present.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- the truth


def _decisions() -> int:
    return len(re.findall(r"^### D-", (ROOT / "docs/DECISIONS.md").read_text(), re.M))


def _build_log() -> int:
    return len(re.findall(r"^## B-", (ROOT / "docs/BUILD-LOG.md").read_text(), re.M))


def _mutants() -> int:
    """Counted from the harness, not from a note about it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_mut", ROOT / "tools" / "mutate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return len(mod.MUTANTS)


def _fixtures() -> int:
    return len(list((ROOT / "fixtures").glob("*.json")))


def _tests() -> int:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                          "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    return sum(int(m) for m in re.findall(r"^tests/\S+: (\d+)$", out, re.M))


def _claim_types() -> int:
    sys.path.insert(0, str(ROOT))
    from app.models import ClaimType
    return len(list(ClaimType))


def _registry() -> int:
    sys.path.insert(0, str(ROOT))
    from app.verify import REGISTRY
    return len(REGISTRY)


def _registry_block() -> str:
    """The TDD prints the registry as source. Compare the NAMES, not the count.

    The block showed twelve entries after `identity` and `sizing` were added
    (B-37) — a document displaying code that the code does not agree with, which
    is worse than no code block at all. Counting would have missed a swap.
    """
    sys.path.insert(0, str(ROOT))
    from app.verify import REGISTRY
    real = sorted(k.name for k in REGISTRY)
    doc = (ROOT / "docs/TDD.md").read_text(encoding="utf-8")
    m = re.search(r"REGISTRY: dict\[ClaimType, Verifier\] = \{(.*?)\}", doc, re.S)
    if m is None:
        raise ValueError("the TDD no longer prints the registry")
    shown = sorted(set(re.findall(r"\b([A-Z][A-Z_]+):", m.group(1))))
    if shown != real:
        missing = [x for x in real if x not in shown]
        extra = [x for x in shown if x not in real]
        raise ValueError(
            f"TDD registry block is out of date — missing {missing or 'none'}, "
            f"stale {extra or 'none'}")
    return f"{len(real)} entries, names match"


def _observed(stat: str) -> Callable[[], object]:
    """Facts derived from the labelled observation set itself.

    B-84/B-85 were both this shape: a number computed once from the data, copied
    into prose and prompts, and then the data grew. The message count went 477 ->
    485 in fourteen places including a system prompt; the seller-directed rate
    was quoted as 17% (the MIDDLE SEGMENT) and as "16-20%" (unsupported at
    either end) when the pooled figure is 14%. Deriving them here means the
    files are the source and the prose is the copy.
    """
    import json as _json

    def go() -> object:
        rows = []
        for f in ("triage_test", "triage_extra_batch0", "triage_extra_batch2"):
            for line in (ROOT / f"evals/data/{f}.jsonl").read_text().splitlines():
                if line.strip() and "_meta" not in line:
                    rows.append(_json.loads(line))
        if stat == "n":
            return len(rows)
        if stat == "seller_directed_pct":
            return round(100 * sum(r["seller_directed"] for r in rows) / len(rows))
        if stat == "highlight_equals_qmark":
            return sum(r["highlighted"] == ("?" in r["text"]) for r in rows)
        raise ValueError(stat)
    return go


def _triage(arm: str, stat: str, n: int = 161) -> Callable[[], float]:
    """A Spike 2 arm figure, from the run that produced it.

    B-101: every one of these lived only in prose, and every one was computed
    against a weights file refit before the docs were written. The arms are
    deterministic up to A2, so nothing was stochastic — a table was copied
    forward past a refit and nothing could notice.
    """
    def go() -> float:
        f = ROOT / f"evals/results/triage_{n}.json"
        if not f.exists():
            flag = " --both-platforms" if n != 161 else ""
            raise FileNotFoundError(
                f"no {n}-row triage result — run "
                f"`uv run python -m evals.run_triage --no-llm{flag}`")
        v = json.loads(f.read_text())["arms"][arm][stat]
        # tp/fp/fn are COUNTS; r/p/f1 are rates. Multiplying a count by 100 is
        # how "33 false positives" became "3300" and reported the doc as stale.
        return v if stat in ("tp", "fp", "fn") else round(100 * v, 1)
    return go


def _triage_meta(key: str) -> Callable[[], object]:
    def go() -> object:
        f = ROOT / "evals/results/triage_161.json"
        return json.loads(f.read_text())[key]
    return go


def _spike1_spread(model: str, arm: str, axis: str, which: str,
                   arms: tuple[str, ...] = ("S0", "S1", "S2")) -> Callable[[], float]:
    """The LOW or HIGH end of an arm's range across every recorded run.

    B-100. The published table was a splice of two runs. A fact pinned to one
    run's value would reintroduce exactly that; these are bounds over all of
    them, so adding a run that falls outside the quoted range breaks the check.
    """
    def go() -> float:
        vals = []
        for f in sorted((ROOT / "evals/results").glob("spike1_*.json")):
            d = json.loads(f.read_text())
            # B-113: group the way `report_spike1` groups — by model AND arm
            # set. Pooling by model alone mixes a three-arm run with a two-arm
            # one, which is the splice this whole mechanism exists to prevent,
            # reintroduced inside the checker meant to catch it.
            # B-119: `report_spike1` keys on (model, arms, CASE COUNT); this
            # was missing the third, so the two-case smoke run B-113 names as
            # the hazard still polluted the checker — which then reported the
            # published range as stale and told a reader to write 0.0%.
            # B-133: count per ARM, not the union. The MUTE control sends one
            # fixed string so all its rows share an empty case_id, and the
            # union was 89 real cases + 1 collapsed MUTE id. Harmless against
            # a `< 50` guard, but this comment claims to group the way
            # `report_spike1` groups and it has to keep being true.
            per_arm: dict[str, set] = collections.defaultdict(set)
            for r in d["rows"]:
                per_arm[r["arm"]].add(r["case_id"])
            cases = max((len(v) for v in per_arm.values()), default=0)
            if (d["draft_model"] != model or tuple(d.get("arms", [])) != arms
                    or cases < 50):
                continue
            sub = [r for r in d["rows"] if r["arm"] == arm]
            if not sub:
                continue
            if axis == "safe":
                xs = [True if r["blocked"] else
                      (None if r["asserts"] is None else not r["asserts"])
                      for r in sub]
            else:
                xs = [r[axis] for r in sub]
            xs = [x for x in xs if x is not None]
            if xs:
                vals.append(100 * sum(xs) / len(xs))
        if not vals:
            raise FileNotFoundError(f"no recorded runs for {model}/{arm}")
        return round(min(vals) if which == "lo" else max(vals), 1)
    return go


def _chi_p() -> float:
    """The homogeneity p-value, COMPUTED from the four per-segment recalls.

    B-115. `evals/stats.py` proved this was 0.130 and `tests/test_stats.py`
    asserts it, while **0.097 stayed in three shipped documents** — the wave
    that found the error corrected the BUILD-LOG entry and not the claims.
    A number that a test disproves and a document still states is worse than an
    unchecked one, because the repo now contradicts itself in public.
    """
    sys.path.insert(0, str(ROOT))
    from evals.stats import chi_square_homogeneity
    return round(chi_square_homogeneity(
        [(4, 10), (11, 27), (22, 32), (6, 10)]).p, 3)


def _bench(path: str, stat: str) -> Callable[[], float]:
    """A latency percentile from the recorded bench run."""
    def go() -> float:
        f = ROOT / "evals/results/bench.json"
        if not f.exists():
            raise FileNotFoundError(
                "no bench result — run `uv run python -m evals.bench --paths free`")
        return json.loads(f.read_text())[path][stat]
    return go


def _spike1(model: str, arm: str, axis: str) -> Callable[[], float]:
    """A rate from a recorded ablation run, so the PRD cannot drift from it.

    Reads the results JSON the eval writes rather than re-running: a doc check
    that costs money and ten minutes is a doc check nobody runs.
    """
    def go() -> float:
        hits = sorted((ROOT / "evals/results").glob(f"spike1_{model}*_b1.json"))
        if not hits:
            raise FileNotFoundError(
                f"no ablation result for {model} — run "
                f"`uv run python -m evals.run_spike1 --suite b1`")
        rows = json.loads(hits[-1].read_text())["rows"]
        sub = [r for r in rows if r["arm"] == arm]
        if axis == "safe":
            vals = [True if r["blocked"] else
                    (None if r["asserts"] is None else not r["asserts"]) for r in sub]
        else:
            vals = [r[axis] for r in sub]
        vals = [v for v in vals if v is not None]
        return round(100 * sum(vals) / len(vals), 1)
    return go


# Facts that are COUNTS of repository contents rather than measurements. These
# move on every commit and `--fix` may rewrite them. Nothing derived from a run
# is in here: an eval result that edited itself into the docs would defeat the
# entire purpose of the check.
_DERIVED_COUNTS = {"tests", "fixtures", "build-log entries", "decisions"}
# B-119. "observed messages" came OUT. It is a count of repository contents in
# one sense and the DENOMINATOR of a measured proportion in another: one of its
# claim sites is `pooled 69/485`, and `--fix` rewrote the 485 while leaving the
# 69, silently changing a published rate from 14.2% to 13.8% under a sentence
# still saying 14%. Its own help text promised "a measurement is never
# auto-edited". If the corpus grows, that rate must be recomputed by a person.


@dataclass
class Fact:
    name: str
    truth: Callable[[], object]
    claims: list[tuple[str, str]] = field(default_factory=list)
    """(file, regex). The regex must capture the stated value in group 1."""
    tolerance: float = 0.0


FACTS = [
    # The ablation numbers, read from the run that produced them. A doc check
    # that re-runs a model-in-the-loop suite costs money and ten minutes, so
    # nobody runs it; reading the recorded result is what makes this cheap
    # enough to be a habit.
    Fact("verify p95 CPU", _bench("verify — CPU (the work)", "p95"),
         [("docs/TDD.md", r"at \*\*(\d\.\d) ms p95 of CPU\*\*"),
          ("docs/PRD.md", r"Measured false: (\d\.\d) ms p95 of CPU"),
          ("docs/DECISIONS.md", r"`verify` at (\d\.\d) ms p95 of CPU"),
          ("app/precompute.py", r"verification costs (\d\.\d) ms p95 of CPU"),
          ("docs/SUBMISSION.md", r"it is (\d\.\d+) ms p95 CPU over the real"),
          ("docs/RESULTS.md", r"verify — CPU \(the work\)\*\* \| 1188 \| 0\.638 \| \*\*(\d\.\d+)\*\*")],
         tolerance=0.35),
    Fact("observed messages", _observed("n"),
         [("docs/PRD.md", r"pooled 69/(\d+)"),
          ("app/triage.py", r"across (\d+) observed messages"),
          ("app/llm.py", r"measured sample of (\d+) real messages")]),
    Fact("seller-directed %", _observed("seller_directed_pct"),
         [("docs/PRD.md", r"\*\*(\d+)% of\s*\n?messages are directed at the seller\*\*"),
          ("app/llm.py", r"only (\d+)% directed at the seller")]),
    Fact("highlighted == '?' agreement", _observed("highlight_equals_qmark"),
         [("evals/run_triage.py", r"messages, every highlighted row contains `\?` and no unhighlighted\s*\n\s*row does\. (\d+)/485")]),
    # --- the CONTESTED numbers. An adversarial pass observed that this file
    # checked 25 facts and "none of the contested ones" — decision counts and
    # heading counts were never in doubt; the Spike 2 arms and the ablation
    # spread were wrong in four documents at once.
    Fact("chi-square homogeneity p", _chi_p,
         [("README.md", r"chi-square homogeneity p = (\d\.\d+)"),
          ("docs/SUBMISSION.md", r"over four segments, p = (\d\.\d+)"),
          ("docs/TDD.md", r"four segments gives \*\*p = (\d\.\d+)\*\*")],
         tolerance=0.0005),
    Fact("A1 precision", _triage("A1", "p"),
         [("README.md", r"\| A1 \+ stage-1 gate \| 88\.9% \| (\d+\.\d)% \|"),
          ("docs/TDD.md", r"\| A1 \+ stage-1 gate \| 88\.9% \| (\d+\.\d)% \|")],
         tolerance=0.05),
    Fact("A1 F1", _triage("A1", "f1"),
         [("README.md", r"\| A1 \+ stage-1 gate \| 88\.9% \| 46\.2% \| (\d+\.\d)% \|"),
          ("docs/TDD.md", r"\| A1 \+ stage-1 gate \| 88\.9% \| 46\.2% \| (\d+\.\d)% \|")],
         tolerance=0.05),
    Fact("A1 precision, 189 two-platform", _triage("A1", "p", 189),
         [("docs/TDD.md", r"A1 gate alone\s+P (\d+\.\d)%"),
          ("docs/RESULTS.md", r"\| A1 \| `[█░]+` (\d+\.\d)%")], tolerance=0.05),
    Fact("A1 false positives, 189", _triage("A1", "fp", 189),
         [("README.md", r"the gate emits \*\*(\d+)\*\* false positives"),
          ("docs/SUBMISSION.md", r"gate emits\s*\n?\s*\*\*(\d+)\*\* false positives"),
          ("docs/TDD.md", r"A1 gate alone\s+P 50\.0%\s+R 89\.2%\s+F1 64\.1%\s+(\d+) false"),
          ("docs/RESULTS.md", r"\| A1 \|[^|]+\|[^|]+\| 64\.1% \| 33 \| (\d+) \|")]),
    Fact("A0 recall", _triage("A0", "r"),
         [("docs/TDD.md", r"question-mark regex \*\(the incumbent\)\* \| (\d+\.\d)%")],
         tolerance=0.05),
    Fact("n_train", _triage_meta("n_train"),
         [("docs/TDD.md", r"Weights are fit on (\d+) messages"),
          ("app/triage.py", r"fit on (\d+) labelled messages"),
          ("evals/fit_triage.py", r"milliseconds on (\d+) rows")]),
    Fact("spike1 sonnet S1 safe, low", _spike1_spread("claude-sonnet-5", "S1", "safe", "lo"),
         [("docs/TDD.md", r"S1  \+ grounding\s+96\.6% \[(\d+\.\d)%")], tolerance=0.05),
    Fact("spike1 sonnet S2 safe, low", _spike1_spread("claude-sonnet-5", "S2", "safe", "lo"),
         [("docs/TDD.md", r"S2  \+ verification\s+96\.6% \[(\d+\.\d)%")], tolerance=0.05),
    Fact("spike1 sonnet S0 safe, high", _spike1_spread("claude-sonnet-5", "S0", "safe", "hi"),
         [("docs/TDD.md", r"S0  bare model\s+49\.4% \[\d+\.\d% - (\d+\.\d)%\]")],
         tolerance=0.05),
    # B-134. RESULTS.md restates every headline figure, so it is pinned the
    # same way the others are. A results page that can drift is the next stale
    # document by construction.
    Fact("spike1 S0 safe high, RESULTS", _spike1_spread("claude-sonnet-5", "S0", "safe", "hi"),
         [("docs/RESULTS.md", r"\| S0 \| `[█░]+` (\d+\.\d)%")], tolerance=0.05),
    Fact("spike1 S1 safe low, RESULTS", _spike1_spread("claude-sonnet-5", "S1", "safe", "lo"),
         [("docs/RESULTS.md", r"\| S1 \|[^|]+\| (\d+\.\d)–")], tolerance=0.05),
    Fact("spike1 S2 safe low, RESULTS", _spike1_spread("claude-sonnet-5", "S2", "safe", "lo"),
         [("docs/RESULTS.md", r"\| S2 \|[^|]+\| (\d+\.\d)–")], tolerance=0.05),
    Fact("decisions", _decisions,
         [("docs/TDD.md", r"`DECISIONS\.md` holds (\d+) decisions"),
          ("docs/SUBMISSION.md", r"\| (\d+) decisions, each with the alternative")]),
    Fact("build-log entries", _build_log,
         [("docs/SUBMISSION.md", r"\| (\d+) entries\. Every bug worth remembering")]),
    Fact("registry block matches the code", _registry_block, [], tolerance=0),
    Fact("fixtures", _fixtures,
         [("README.md", r"against (\d+) recorded fixtures"),
          ("README.md", r"fixtures/\s+(\d+) recorded LLM responses"),
          ("docs/PRD.md", r"built — (\d+) fixtures"),
          # B-128. These two were the SAME number in the same repo and neither
          # was pinned, so `--fix` rewrote three cells green while two others
          # kept saying 288. Pinning the count is not enough; every place it is
          # WRITTEN has to be pinned, which is what --audit exists to find.
          ("docs/SUBMISSION.md", r"replays (\d+) recorded"),
          ("docs/TDD.md", r"\| (\d+) fixtures; the tape raises"),
          ("docs/RESULTS.md", r"\| recorded fixtures \| \*\*(\d+)\*\*"),
          ("docs/RESULTS.md", r"\| \*\*D\*\* unit \+ golden replay \|[^|]+\| (\d+) fixtures")]),
    # B-130. The mutant count is derived from the harness, so adding a rule
    # without a mutant for it cannot quietly leave the README claiming the old
    # number — which is how "41/41" would have outlived the 41.
    Fact("mutants", _mutants,
         [("README.md", r"mutate\.py\s+# (\d+) mutants"),
          ("docs/RESULTS.md", r"\| mutation \| \*\*(\d+) / \d+\*\* mutants killed")]),
    Fact("tests", _tests,
         [("README.md", r"uv run pytest\s+# (\d+) tests"),
          ("docs/RESULTS.md", r"\| tests \| \*\*(\d+)\*\*"),
          ("docs/SUBMISSION.md", r"\*\*(\d+) tests\*\*, no credential")]),

]


# Numbers that are prose, not measurements: dates, section refs, code
# identifiers, money in illustrative examples, and the B-NN / D-NN citations.
_PROSE = __import__("re").compile(
    r"B-\d+|D-\d+|§\s*\d+|20\d\d(?:-\d\d)?|"          # citations, dates
    r"\bp\.?\s*\d+|\b[vV]\d+\b|\b\d+\s*(?:KB|MB|ms|s|px|pt)\b|"
    r"\b(?:lot_|itm_|f)\d+|\d+/\d+\s*(?:102|025|112|82)\b")


def audit_unpinned(files: list[str], pinned: set[str]) -> list[tuple[str, str]]:
    """Every number in a document that NOTHING checks.

    B-123. `check_docs` verified 25 of ~695 numeric tokens across the four
    documents — **3.6%** — so the default was *unchecked unless pinned*, and
    three separate false claims survived five waves of review by living in the
    96% nobody looked at:

      - "a bare model is 46.1% safe" appears in **no recorded run**; it is
        `100 - 53.9`, a model's SAFE rate transcribed into an UNSAFE column and
        then propagated into four documents as measured fact.
      - "+2.3, p = 0.016" still led the two files the README labels "Start
        here" while the TDD spent a page retracting it as a splice.
      - "35 of 37 false positives" is a subtraction from a population that does
        not exist — the shipped gate emits 33, and `run_triage.py` says so.

    Each time, the fix corrected the cell a Fact pinned and left the claim
    everywhere else. Inverting the default is the only version of this check
    that could have caught them: a number is suspect until something derives it.

    This does not make the numbers right. It makes the unchecked ones VISIBLE,
    which is the difference between an audit and a formality.
    """
    import re as _re
    out = []
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for m in _re.finditer(r"\b\d[\d,]*(?:\.\d+)?%?", text):
            tok = m.group(0)
            ctx = text[max(0, m.start() - 60):m.end() + 30].replace("\n", " ")
            if _PROSE.search(ctx) or tok in pinned:
                continue
            out.append((rel, ctx.strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true",
                    help="list every numeric token no Fact derives (B-123)")
    ap.add_argument("--fix", action="store_true",
                    help="rewrite DERIVED counts in place. Only counts — a "
                         "measurement is never auto-edited, because a number "
                         "that silently updates itself is a number nobody "
                         "reads, and the point of a stale-doc check is to make "
                         "someone look at what moved.")
    a = ap.parse_args()
    problems, checked, unmatched, fixed = [], 0, [], 0
    for fact in FACTS:
        try:
            truth = fact.truth()
        except Exception as exc:                     # noqa: BLE001
            problems.append(f"  CANNOT DERIVE  {fact.name}: {exc}")
            continue
        for rel, pattern in fact.claims:
            p = ROOT / rel
            if not p.exists():
                problems.append(f"  MISSING FILE   {rel}")
                continue
            m = re.search(pattern, p.read_text(encoding="utf-8"), re.S)
            if m is None:
                unmatched.append(f"  claim not found  {rel}: /{pattern}/ "
                                 f"(fact {fact.name} = {truth})")
                continue
            checked += 1
            stated = m.group(1)
            ok = (abs(float(stated) - float(truth)) <= fact.tolerance
                  if fact.tolerance else str(stated) == str(truth))
            if not ok:
                # Counts of things in the repo are bookkeeping and go stale on
                # every commit; measurements are results and must be looked at.
                if a.fix and fact.name in _DERIVED_COUNTS:
                    text = p.read_text(encoding="utf-8")
                    lo, hi = m.span(1)
                    p.write_text(text[:lo] + str(truth) + text[hi:],
                                 encoding="utf-8")
                    fixed += 1
                    continue
                problems.append(
                    f"  STALE          {rel}: says {stated}, "
                    f"{fact.name} is {truth}")

    if a.audit:
        pinned = set()
        for fact in FACTS:
            try:
                pinned.add(str(fact.truth()))
            except Exception:                        # noqa: BLE001
                pass
        rows = audit_unpinned(
            ["README.md", "docs/PRD.md", "docs/TDD.md", "docs/SUBMISSION.md"],
            pinned)
        by: dict[str, int] = {}
        for rel, _ in rows:
            by[rel] = by.get(rel, 0) + 1
        print("\n  UNPINNED numeric tokens — nothing in the repo derives these\n")
        for rel, n in sorted(by.items()):
            print(f"      {rel:<24}{n:>5}")
        print(f"      {'TOTAL':<24}{len(rows):>5}\n")
        return 0

    for line in problems + unmatched:
        print(line)
    print(f"\n  {checked} claims checked · {len(problems)} stale · "
          f"{len(unmatched)} not found"
          + (f" · {fixed} counts updated" if fixed else ""))
    # B-107. `unmatched` used to be a warning and the exit code ignored it, so
    # a reworded sentence silently stopped being checked — and the docstring
    # claimed that case was "the other half of the guarantee". In exit-code
    # terms, which is the only thing CI reads, it passed silently. A claim the
    # checker can no longer find is a claim nobody is checking, and that is a
    # failure whether the prose was rewritten deliberately or not: the fix is to
    # update the regex here, deliberately, in the same commit.
    return 1 if (problems or unmatched) else 0


if __name__ == "__main__":
    raise SystemExit(main())
