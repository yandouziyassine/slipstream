from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from slipstream.kraken_rest import Bar
from slipstream.models import (
    AlmgrenChrissParams,
    BookUpdate,
    OrderSpec,
    PovParams,
    ScheduleParams,
    TwapParams,
    VwapParams,
)

SECONDS_PER_DAY = 86_400
BUCKET_S = 900
BUCKETS_PER_DAY = SECONDS_PER_DAY // BUCKET_S
MIN_SIGMA_BARS = 60
MAX_IMPACT_LEVELS = 10
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


def estimate_eta(liquidity: Sequence[tuple[float, float]], tau_s: float) -> float:
    if not liquidity:
        raise CalibrationError("empty book side")
    if not math.isfinite(tau_s) or tau_s <= 0:
        raise CalibrationError("invalid interval")
    for price, qty in liquidity:
        if not (math.isfinite(price) and price > 0 and math.isfinite(qty) and qty > 0):
            raise CalibrationError("invalid book level")
    touch = liquidity[0][0]
    cum_qty = 0.0
    cum_notional = 0.0
    sizes: list[float] = []
    costs: list[float] = []
    for price, qty in liquidity[:MAX_IMPACT_LEVELS]:
        cum_qty += qty
        cum_notional += price * qty
        avg_price = cum_notional / cum_qty
        sizes.append(cum_qty)
        costs.append(abs(avg_price - touch))
    if len(sizes) < MIN_IMPACT_POINTS:
        raise CalibrationError("book too thin to estimate impact")
    slope = statistics.linear_regression(sizes, costs).slope
    if not math.isfinite(slope) or slope <= 0:
        raise CalibrationError("non-positive impact slope")
    return slope * tau_s


@dataclass(frozen=True)
class CalibrationData:
    bars_15m: tuple[Bar, ...]
    bars_1m: tuple[Bar, ...]


def schedule_params(
    spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
) -> ScheduleParams:
    if spec.algo == "twap":
        return TwapParams()
    if spec.algo == "pov":
        return PovParams(spec.participation)
    if data is None:
        raise CalibrationError(f"{spec.algo} needs OHLC calibration data")
    if spec.algo == "vwap":
        return VwapParams(vwap_weights(data.bars_15m, start_ns, spec.duration_s, spec.num_slices))
    liquidity = sorted(book.asks) if spec.side == "buy" else sorted(book.bids, reverse=True)
    tau_s = spec.duration_s / spec.num_slices
    eta = estimate_eta(liquidity, tau_s)
    sigma = estimate_sigma(data.bars_1m)
    risk_aversion = (
        spec.risk_aversion
        if spec.risk_aversion is not None
        else URGENCY_RISK_AVERSION[spec.urgency]
    )
    return AlmgrenChrissParams(sigma, eta, risk_aversion)
