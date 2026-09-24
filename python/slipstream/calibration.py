from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence

from slipstream.kraken_rest import Bar

SECONDS_PER_DAY = 86_400
BUCKET_S = 900
BUCKETS_PER_DAY = SECONDS_PER_DAY // BUCKET_S
MIN_SIGMA_BARS = 60
IMPACT_MULTIPLIERS = (0.5, 1.0, 2.0, 4.0)
MIN_IMPACT_POINTS = 3
URGENCY_RISK_AVERSION = {"low": 3e-6, "medium": 3e-5, "high": 3e-4}


class CalibrationError(ValueError):
    pass


def vwap_weights(
    bars_15m: Sequence[Bar], start_ns: int, duration_s: int, slices: int
) -> tuple[float, ...]:
    if start_ns < 0 or duration_s <= 0 or slices < 1:
        raise CalibrationError("invalid VWAP horizon")
    totals = [0.0] * BUCKETS_PER_DAY
    counts = [0] * BUCKETS_PER_DAY
    for bar in bars_15m:
        bucket = (bar.time_s % SECONDS_PER_DAY) // BUCKET_S
        totals[bucket] += bar.volume
        counts[bucket] += 1
    profile = [totals[i] / counts[i] if counts[i] else 0.0 for i in range(BUCKETS_PER_DAY)]
    observed = [volume for volume in profile if volume > 0]
    if not observed:
        raise CalibrationError("no volume history for VWAP profile")
    fallback = sum(observed) / len(observed)
    interval_s = duration_s / slices
    start_s = start_ns / 1e9
    weights = []
    for k in range(slices):
        midpoint_s = start_s + (k + 0.5) * interval_s
        bucket = int(midpoint_s % SECONDS_PER_DAY) // BUCKET_S
        weights.append(profile[bucket] if profile[bucket] > 0 else fallback)
    return tuple(weights)


def estimate_sigma(bars_1m: Sequence[Bar]) -> float:
    if len(bars_1m) < MIN_SIGMA_BARS:
        raise CalibrationError(f"need at least {MIN_SIGMA_BARS} one-minute bars")
    closes = [bar.close for bar in sorted(bars_1m, key=lambda b: b.time_s)]
    changes = [later - earlier for earlier, later in itertools.pairwise(closes)]
    sigma = statistics.stdev(changes) / math.sqrt(60)
    if not math.isfinite(sigma) or sigma <= 0:
        raise CalibrationError("zero or invalid volatility estimate")
    return sigma


def estimate_eta(liquidity: Sequence[tuple[float, float]], slice_qty: float, tau_s: float) -> float:
    if not liquidity:
        raise CalibrationError("empty book side")
    if not (math.isfinite(slice_qty) and slice_qty > 0 and math.isfinite(tau_s) and tau_s > 0):
        raise CalibrationError("invalid slice size or interval")
    touch = liquidity[0][0]
    sizes: list[float] = []
    costs: list[float] = []
    for multiplier in IMPACT_MULTIPLIERS:
        qty = slice_qty * multiplier
        filled, avg_price = _walk(liquidity, qty)
        if filled < qty * (1 - 1e-9):
            break
        sizes.append(qty)
        costs.append(abs(avg_price - touch))
    if len(sizes) < MIN_IMPACT_POINTS:
        raise CalibrationError("book too thin to estimate impact")
    slope = statistics.linear_regression(sizes, costs).slope
    if not math.isfinite(slope) or slope <= 0:
        raise CalibrationError("non-positive impact slope")
    return slope * tau_s


def _walk(liquidity: Sequence[tuple[float, float]], qty: float) -> tuple[float, float]:
    remaining = qty
    filled = 0.0
    notional = 0.0
    for price, level_qty in liquidity:
        if remaining <= 0:
            break
        take = min(remaining, level_qty)
        filled += take
        notional += take * price
        remaining -= take
    return filled, (notional / filled if filled > 0 else 0.0)
