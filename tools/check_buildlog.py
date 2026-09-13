#!/usr/bin/env python3
"""Every B-NN referenced in the source must be written up in the BUILD-LOG.

A code in a comment that resolves to nothing is worse than no code: it reads
like a citation and is not one. This runs in CI-shape (exit non-zero) so the
divergence cannot come back.

    uv run python tools/check_buildlog.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "BUILD-LOG.md"


def main() -> int:
    doc = DOC.read_text(encoding="utf-8")
    # A code is documented if it has its own heading OR a row in the sub-code
    # index — B-42 and B-56 each cover one adversarial pass and would otherwise
    # need thirty headings of three lines.
    # B-114: these were `B-\d\d`, so the moment the log crossed 100 every
    # three-digit citation silently truncated — `B-100` matched as `B-10`, which
    # IS documented, so both resolved to an unrelated entry and the tool
    # reported success. Its own docstring says "a code that resolves to nothing
    # is worse than no code: it reads like a citation and is not one." A code
    # that resolves to the WRONG one is worse still.
    documented = set(re.findall(r"^## (B-\d{2,})", doc, re.M))
    documented |= set(re.findall(r"^\| (B-\d{2,}) \|", doc, re.M))

    out = subprocess.run(
        ["grep", "-rhoE", "B-[0-9]{2,}", "app", "tests", "evals", "tools"],
        cwd=ROOT, capture_output=True, text=True).stdout
    used = set(re.findall(r"B-\d{2,}", out))

    missing = sorted(used - documented)
    orphan = sorted(d for d in documented - used if d > "B-32")

    for code in missing:
        print(f"  UNDOCUMENTED  {code} is cited in the source and not in BUILD-LOG.md")
    for code in orphan:
        print(f"  note          {code} is documented but no longer cited "
              f"(fine if the code it described was deleted)")
    print(f"\n  {len(used)} codes cited · {len(documented)} documented · "
          f"{len(missing)} unresolved")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
