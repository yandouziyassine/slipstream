from __future__ import annotations

import pytest

from slipstream.site.stats import StatsError, diff_ci, median_ci, verdict


def test_median_ci_single_value_is_a_point() -> None:
    assert median_ci([5.0]) == (5.0, 5.0, 5.0)


def test_median_ci_identical_values_has_a_zero_width_band() -> None:
    median, lo, hi = median_ci([3.0] * 20)
    assert (median, lo, hi) == (3.0, 3.0, 3.0)


def test_median_ci_hand_checked_tiny_sample_fixed_seed() -> None:
    # n_boot=41 makes both the 2.5th and 97.5th percentile land on an exact index (1 and 39 of
    # 0..40), so the interpolation in _percentile is a no-op and the resample sequence for
    # random.Random(7) can be hand-verified: sorting the 41 resample medians gives eleven 1.0s,
    # nineteen 2.0s and eleven 3.0s, so index 1 is 1.0 and index 39 is 3.0.
    median, lo, hi = median_ci([1.0, 2.0, 3.0], n_boot=41, seed=7)
    assert (median, lo, hi) == (2.0, 1.0, 3.0)


def test_median_ci_rejects_empty_sample() -> None:
    with pytest.raises(StatsError):
        median_ci([])


def test_diff_ci_rejects_an_empty_sample() -> None:
    with pytest.raises(StatsError):
        diff_ci([1.0], [])


def test_verdict_identical_samples_are_not_distinguishable() -> None:
    result = verdict("a", [40.0, 40.1, 39.9, 40.2] * 5, "b", [40.0, 40.1, 39.9, 40.2] * 5)
    assert result.startswith("not yet distinguishable")
    assert "n = 40" in result


def test_verdict_clearly_separated_samples_names_the_cheaper_one() -> None:
    a = [10.0 + 0.01 * i for i in range(30)]
    b = [1.0 + 0.01 * i for i in range(30)]
    assert verdict("a", a, "b", b) == "b cheaper"
    assert verdict("b", b, "a", a) == "b cheaper"


def test_verdict_reports_n_when_a_sample_is_empty() -> None:
    assert verdict("a", [], "b", [1.0]) == "not yet distinguishable (n = 1)"
