"""The tests, in code — because a p-value in prose is an assertion (B-87).

`docs/TDD.md` opens with *"Every number here is reproducible from a command in
the repo; none is asserted."* That was false for every p-value in it. An
adversarial pass ran `grep -rni mcnemar --include=*.py` and got nothing: the
statistics were computed once in a scratch session and typed into markdown.

Two of them did not survive recomputation — the chi-square p was **0.130**, not
the 0.097 printed in three places — and nothing in the repo could have noticed.

No scipy. Exact tests on small integer counts need binomial coefficients and a
chi-square survival function, both of which are a dozen lines, and adding a
compiled numerical stack to a project whose whole argument is "you can read
every line" would be its own kind of dishonesty.

Everything here is checked against textbook worked examples in
`tests/test_stats.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# =====================================================================
# McNemar — the right test for paired binary outcomes
# =====================================================================


@dataclass(frozen=True)
class McNemar:
    b: int          # A right, B wrong
    c: int          # B right, A wrong
    p: float
    note: str = ""

    @property
    def discordant(self) -> int:
        return self.b + self.c

    def line(self, label: str = "") -> str:
        return (f"{label + ': ' if label else ''}{self.c} vs {self.b} "
                f"discordant, exact p = {self.p:.4f}"
                f"{'  — ' + self.note if self.note else ''}")


def mcnemar(b: int, c: int) -> McNemar:
    """Exact McNemar. See below — this docstring continues after the guard."""
    """Exact (binomial) McNemar on the discordant pairs.

    Exact rather than the chi-square approximation on purpose: every comparison
    in this repo has fewer than 30 discordant pairs, where the continuity-
    corrected chi-square is not licensed. With b+c = 9 the difference is the
    difference between p = 0.039 and a number you should not quote.

    **Concordant pairs carry no information about the difference** — that is the
    whole point of the test, and it is also the trap: a 16-vs-0 result over 37
    cases and a 16-vs-0 result over 37,000 give the identical p, so a small p
    here says the ASYMMETRY is unlikely by chance, not that the effect is large
    or that the sample is adequate.
    """
    # B-119: `math.comb` raises TypeError on a float, which is the right
    # outcome but an opaque one; counts of discordant PAIRS are integers by
    # construction and a caller passing otherwise has a bug worth naming.
    if int(b) != b or int(c) != c or b < 0 or c < 0:
        raise ValueError(f"discordant pair counts must be non-negative "
                         f"integers, got b={b!r} c={c!r}")
    b, c = int(b), int(c)
    n = b + c
    if n == 0:
        return McNemar(b, c, 1.0, "no discordant pairs — the test is vacuous")
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    p = min(1.0, 2 * tail)
    # B-112: these used to overwrite rather than accumulate, so `mcnemar(0, 3)`
    # — three discordant pairs, about as underpowered as a test can be — lost
    # its warning entirely to the one-sided note. The existing test probed (1,8)
    # and (8,12), neither of which has a zero cell, so it was written around the
    # bug rather than at it.
    notes = []
    if n < 10:
        notes.append(f"only {n} discordant pairs — underpowered")
    if 0 in (b, c):
        notes.append(f"one-sided by construction (b={b}, c={c}) — check whether "
                     f"that is an empirical finding or forced by the design")
    return McNemar(b, c, p, "; ".join(notes))


# =====================================================================
# Wilson score interval — for a proportion, never mean +/- 1.96 sigma
# =====================================================================


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI for k successes in n trials.

    Wilson rather than the normal approximation because these proportions live
    near 1 and n is small: 10/10 has a normal interval of [1.0, 1.0], which is
    absurd, and a Wilson interval of [72.2%, 100%], which is the honest answer
    and is why Suite E's `10/10` means less than it looks like.
    """
    if n == 0:
        return (0.0, 0.0)
    if k > n or k < 0:
        raise ValueError(f"wilson: {k} successes in {n} trials is not a proportion")
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


# =====================================================================
# Chi-square homogeneity, with the assumption check the last one skipped
# =====================================================================


@dataclass(frozen=True)
class ChiSquare:
    stat: float
    dof: int
    p: float
    min_expected: float

    @property
    def licensed(self) -> bool:
        """Cochran's rule: the asymptotic test needs every expected cell >= 5."""
        return self.min_expected >= 5.0

    def line(self) -> str:
        warn = "" if self.licensed else (
            f"  — NOT LICENSED: min expected cell {self.min_expected:.2f} < 5, "
            f"use an exact test")
        return f"chi2({self.dof}) = {self.stat:.3f}, p = {self.p:.4f}{warn}"


def chi_square_homogeneity(counts: list[tuple[int, int]]) -> ChiSquare:
    """Are these k groups drawn from one distribution? `counts` is [(hit, n)].

    Reports `min_expected` so the caller can see whether the asymptotic
    approximation applies. The previous use of this test in the project had a
    minimum expected cell of 4.56 and said nothing about it.
    """
    k = len(counts)
    if not k:
        # B-119: `min_exp` started at `inf` and was never reset when the loop
        # body did not run, so an EMPTY input reported `licensed = True` —
        # satisfying Cochran's rule with no data at all.
        return ChiSquare(0.0, 0, 1.0, 0.0)
    tot_hit = sum(h for h, _ in counts)
    tot_n = sum(n for _, n in counts)
    p_pool = tot_hit / tot_n if tot_n else 0.0
    stat = 0.0
    min_exp = float("inf")
    for hit, n in counts:
        for observed, expected in ((hit, n * p_pool), (n - hit, n * (1 - p_pool))):
            min_exp = min(min_exp, expected)
            if expected > 0:
                stat += (observed - expected) ** 2 / expected
    dof = k - 1
    if dof < 1:
        # B-112. One group has nothing to be homogeneous WITH, and `_chi2_sf`
        # divides by `k/2` — so this raised ZeroDivisionError on 230 of the 820
        # single-group inputs swept, whenever floating-point residue in the
        # second cell made `stat` a tiny positive number instead of exactly
        # zero. A degenerate question deserves a degenerate answer, not a crash.
        return ChiSquare(stat, 0, 1.0, min_exp)
    return ChiSquare(stat, dof, _chi2_sf(stat, dof), min_exp)


def _chi2_sf(x: float, k: int) -> float:
    """P(X > x) for chi-square with k dof, via the regularised upper gamma.

    `math.gamma` plus a continued fraction; accurate to ~1e-12 over the range
    that matters here and checked against published tables in the tests.
    """
    if x <= 0:
        return 1.0
    a = k / 2.0
    x2 = x / 2.0
    if x2 < a + 1:
        # series expansion for the lower incomplete gamma
        term = 1.0 / a
        total = term
        n = 1
        while n < 500:
            term *= x2 / (a + n)
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
            n += 1
        return max(0.0, 1.0 - total * math.exp(-x2 + a * math.log(x2)
                                               - math.lgamma(a)))
    # continued fraction for the upper incomplete gamma
    tiny = 1e-300
    b = x2 + 1 - a
    c = 1 / tiny
    d = 1 / b if b else 1 / tiny
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return h * math.exp(-x2 + a * math.log(x2) - math.lgamma(a))


# =====================================================================
# Spread — the discipline that would have caught B-87
# =====================================================================


@dataclass(frozen=True)
class Spread:
    values: list[float]

    @property
    def lo(self) -> float:
        return min(self.values)

    @property
    def hi(self) -> float:
        return max(self.values)

    @property
    def mid(self) -> float:
        """True median, not the upper-middle element (B-113).

        `sorted(v)[len(v)//2]` is biased high on even n and for n = 2 it IS the
        maximum — so the aggregate written to stop a hand picking the favourable
        number picked it automatically. The haiku S1 figure printed 93.2% where
        the median of its two runs is 91.0%.
        """
        xs = sorted(self.values)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    def line(self, label: str, pct: bool = True) -> str:
        f = (lambda v: f"{v:.1%}") if pct else (lambda v: f"{v:.2f}")
        if len(self.values) == 1:
            return f"{label:<26}{f(self.values[0]):>9}   (ONE RUN — not a rate)"
        return (f"{label:<26}{f(self.mid):>9}   "
                f"[{f(self.lo)} .. {f(self.hi)}] over {len(self.values)} runs")
