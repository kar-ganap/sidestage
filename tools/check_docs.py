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
    Fact("spike1 sonnet S1 safe", _spike1("sonnet-5", "S1", "safe"),
         [("docs/TDD.md", r"S1  \+ grounding\s+(\d+\.\d)%\s+98\.9%")], tolerance=0.05),
    Fact("spike1 sonnet S2 safe", _spike1("sonnet-5", "S2", "safe"),
         [("docs/TDD.md", r"S2  \+ verification\s+(\d+\.\d)%\s+91\.0%")], tolerance=0.05),
    Fact("spike1 sonnet S1 responsive", _spike1("sonnet-5", "S1", "responsive"),
         [("docs/TDD.md", r"S1  \+ grounding\s+94\.3%\s+(\d+\.\d)%")], tolerance=0.05),
    Fact("spike1 sonnet S2 responsive", _spike1("sonnet-5", "S2", "responsive"),
         [("docs/TDD.md", r"S2  \+ verification\s+96\.6%\s+(\d+\.\d)%")], tolerance=0.05),
    Fact("spike1 haiku S1 safe", _spike1("haiku-4-5", "S1", "safe"),
         [("docs/TDD.md", r"S1  \+ grounding\s+(\d+\.\d)%\s+88\.8%")], tolerance=0.05),
    Fact("spike1 haiku S2 safe", _spike1("haiku-4-5", "S2", "safe"),
         [("docs/TDD.md", r"S2  \+ verification\s+(\d+\.\d)%\s+79\.8%")], tolerance=0.05),
    Fact("verify p95 CPU", _bench("verify — CPU (the work)", "p95"),
         [("docs/TDD.md", r"at \*\*(\d\.\d) ms p95 of CPU\*\*"),
          ("docs/PRD.md", r"Measured false: (\d\.\d) ms p95 of CPU"),
          ("docs/DECISIONS.md", r"`verify` at (\d\.\d) ms p95 of CPU"),
          ("app/precompute.py", r"verification costs (\d\.\d) ms p95 of CPU"),
          ("docs/SUBMISSION.md", r"it is (\d\.\d+) ms p95 CPU over the real")],
         tolerance=0.35),
    Fact("decisions", _decisions,
         [("docs/TDD.md", r"`DECISIONS\.md` holds (\d+) decisions"),
          ("docs/SUBMISSION.md", r"\| (\d+) decisions, each with the alternative")]),
    Fact("build-log entries", _build_log,
         [("docs/SUBMISSION.md", r"\| (\d+) entries\. Every bug worth remembering")]),
    Fact("registry block matches the code", _registry_block, [], tolerance=0),
    Fact("fixtures", _fixtures,
         [("README.md", r"against (\d+) recorded fixtures"),
          ("README.md", r"fixtures/\s+(\d+) recorded LLM responses"),
          ("docs/PRD.md", r"built — (\d+) fixtures")]),
    Fact("tests", _tests,
         [("README.md", r"uv run pytest\s+# (\d+) tests"),
          ("docs/SUBMISSION.md", r"\*\*(\d+) tests\*\*, no credential")]),

]


def main() -> int:
    problems, checked, unmatched = [], 0, []
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
                problems.append(
                    f"  STALE          {rel}: says {stated}, "
                    f"{fact.name} is {truth}")

    for line in problems + unmatched:
        print(line)
    print(f"\n  {checked} claims checked · {len(problems)} stale · "
          f"{len(unmatched)} not found")
    # A claim that no longer matches its regex is a warning, not a failure: the
    # prose may legitimately have been rewritten. A claim that matches and
    # disagrees is a failure.
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
