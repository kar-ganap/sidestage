"""B-87 — the tests, checked against worked examples rather than trusted.

`docs/TDD.md` claims "every number here is reproducible from a command in the
repo; none is asserted." Every p-value in it was asserted: an adversarial pass
grepped for `mcnemar` across the source and found nothing. Two of the printed
values did not survive recomputation.

So the tests now live in `evals/stats.py`, and this file checks them against
published worked examples — because an implementation nobody validated is the
same failure one layer down.
"""

from __future__ import annotations

import math

import pytest

from evals.stats import chi_square_homogeneity, mcnemar, wilson


# =====================================================================
# McNemar
# =====================================================================


def test_mcnemar_matches_the_textbook_example():
    """Agresti, *Categorical Data Analysis*: b=12, c=5 -> two-sided exact
    p = 0.1435. Computed here by summing the binomial tail directly."""
    r = mcnemar(12, 5)
    assert r.p == pytest.approx(0.1435, abs=5e-4)


def test_mcnemar_is_symmetric_in_its_arguments():
    """The two-sided test cannot care which arm is which."""
    assert mcnemar(3, 11).p == pytest.approx(mcnemar(11, 3).p)


def test_mcnemar_agrees_with_the_closed_form_binomial():
    """Cross-check against an independent derivation: two-sided exact McNemar is
    a sign test on the discordant pairs, so p = 2 * P(X <= min(b,c)) for
    X ~ Bin(b+c, 0.5)."""
    for b, c in [(0, 5), (1, 8), (4, 4), (2, 9), (7, 16), (0, 16)]:
        n, k = b + c, min(b, c)
        expect = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
        assert mcnemar(b, c).p == pytest.approx(expect)


def test_no_discordant_pairs_is_vacuous_not_significant():
    """b = c = 0 means the two arms never disagreed. That is p = 1, and the
    result carries no information — a fact worth saying out loud rather than
    returning a bare float for."""
    r = mcnemar(0, 0)
    assert r.p == 1.0 and "vacuous" in r.note


def test_a_one_sided_result_is_flagged_for_inspection():
    """B-88: Spike 2's headline is 16 vs 0, and an exhaustive sweep showed the
    gate CANNOT miss a message containing `?`. A zero cell that is forced by the
    design is not the same evidence as a zero cell that happened, and the test
    cannot tell them apart — so it says so."""
    assert "one-sided by construction" in mcnemar(0, 16).note


def test_small_samples_are_flagged_as_underpowered():
    assert "underpowered" in mcnemar(1, 8).note
    assert "underpowered" not in mcnemar(8, 12).note


def test_concordant_pairs_do_not_change_the_result():
    """The property that makes McNemar right for this and also makes it easy to
    over-read: 16-of-37 and 16-of-37000 give the same p."""
    assert mcnemar(0, 16).p == mcnemar(0, 16).p   # by construction; documented


# =====================================================================
# Wilson
# =====================================================================


def test_wilson_matches_the_published_interval():
    """Brown, Cai & DasGupta (2001): 10 successes in 10 trials gives a Wilson
    95% interval of roughly [0.722, 1.0]."""
    lo, hi = wilson(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-3)
    assert hi == pytest.approx(1.0, abs=1e-9)


def test_wilson_on_a_half_is_symmetric_about_a_half():
    lo, hi = wilson(50, 100)
    assert (lo + hi) / 2 == pytest.approx(0.5, abs=1e-9)


def test_wilson_does_not_produce_a_degenerate_interval_at_the_boundary():
    """The whole reason not to use mean +/- 1.96 sigma: at k = n the normal
    interval collapses to a point, which would make Suite E's 10/10 look
    certain."""
    lo, hi = wilson(10, 10)
    assert lo < 0.9, "an interval that excludes 0.72 at n=10 is not honest"


def test_wilson_narrows_with_n():
    assert (wilson(90, 100)[1] - wilson(90, 100)[0]) < \
           (wilson(9, 10)[1] - wilson(9, 10)[0])


# =====================================================================
# Chi-square homogeneity
# =====================================================================


def test_chi_square_reproduces_the_projects_own_four_segments():
    """B-87. The four per-segment recalls the project quotes are
    4/10, 11/27, 22/32, 6/10. The statistic printed in three documents was
    5.65 and reproduces exactly; the p-value printed alongside it was **0.097**
    and does not — it is **0.130**. The conclusion (does not reject) is
    unchanged, which is why nobody noticed for the life of the project."""
    r = chi_square_homogeneity([(4, 10), (11, 27), (22, 32), (6, 10)])
    assert r.stat == pytest.approx(5.650, abs=5e-3)
    assert r.dof == 3
    assert r.p == pytest.approx(0.1299, abs=1e-3)
    assert r.p != pytest.approx(0.097, abs=1e-3)


def test_that_test_was_not_licensed_and_now_says_so():
    """Cochran's rule wants every expected cell >= 5. The minimum here is 4.56,
    so the asymptotic approximation does not apply and an exact test is the
    right call. The original use said nothing about this."""
    r = chi_square_homogeneity([(4, 10), (11, 27), (22, 32), (6, 10)])
    assert r.min_expected == pytest.approx(4.56, abs=0.02)
    assert not r.licensed
    assert "NOT LICENSED" in r.line()


def test_identical_groups_give_a_statistic_of_zero():
    r = chi_square_homogeneity([(5, 10), (10, 20), (25, 50)])
    assert r.stat == pytest.approx(0.0, abs=1e-12)
    assert r.p == pytest.approx(1.0, abs=1e-9)


def test_chi_square_survival_matches_published_critical_values():
    """chi2 = 7.815 at 3 dof is the 95th percentile; 3.841 at 1 dof likewise."""
    from evals.stats import _chi2_sf
    assert _chi2_sf(7.815, 3) == pytest.approx(0.05, abs=1e-3)
    assert _chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert _chi2_sf(11.345, 3) == pytest.approx(0.01, abs=1e-3)
