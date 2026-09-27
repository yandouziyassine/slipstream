from __future__ import annotations

import random
import statistics
from collections.abc import Sequence


class StatsError(ValueError):
    pass


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = q * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def median_ci(
    values: Sequence[float], n_boot: int = 1000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float, float]:
    """Bootstrap 95%-style CI (via alpha) of the median. Returns (median, lo, hi)."""
    if not values:
        raise StatsError("median_ci requires at least one value")
    median = statistics.median(values)
    if len(values) == 1:
        return median, median, median
    rng = random.Random(seed)  # noqa: S311 - bootstrap resampling, not cryptographic
    n = len(values)
    boot = sorted(
        statistics.median(values[rng.randrange(n)] for _ in range(n)) for _ in range(n_boot)
    )
    lo = _percentile(boot, alpha / 2)
    hi = _percentile(boot, 1 - alpha / 2)
    return median, lo, hi


def diff_ci(
    a: Sequence[float], b: Sequence[float], n_boot: int = 1000, seed: int = 7, alpha: float = 0.05
) -> tuple[float, float, float]:
    """Bootstrap CI of median(a) - median(b). Returns (diff, lo, hi)."""
    if not a or not b:
        raise StatsError("diff_ci requires at least one value in each sample")
    diff = statistics.median(a) - statistics.median(b)
    rng = random.Random(seed)  # noqa: S311 - bootstrap resampling, not cryptographic
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(n_boot):
        sample_a = [a[rng.randrange(na)] for _ in range(na)]
        sample_b = [b[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(statistics.median(sample_a) - statistics.median(sample_b))
    diffs.sort()
    lo = _percentile(diffs, alpha / 2)
    hi = _percentile(diffs, 1 - alpha / 2)
    return diff, lo, hi


def verdict(a_name: str, a: Sequence[float], b_name: str, b: Sequence[float]) -> str:
    """Names a cheaper side only when the bootstrap interval for the difference excludes 0."""
    n = len(a) + len(b)
    if not a or not b:
        return f"not yet distinguishable (n = {n})"
    _, lo, hi = diff_ci(a, b)
    if hi < 0:
        return f"{a_name} cheaper"
    if lo > 0:
        return f"{b_name} cheaper"
    return f"not yet distinguishable (n = {n})"
