import math
import random

import pytest

from slipstream.calibration import (
    URGENCY_RISK_AVERSION,
    CalibrationError,
    estimate_eta,
    estimate_sigma,
    vwap_weights,
)
from slipstream.kraken_rest import Bar

DAY0 = 1_700_006_400  # 00:00 UTC (multiple of 86400)
NS = 1_000_000_000


def bar(time_s: int, close: float = 100.0, volume: float = 1.0) -> Bar:
    return Bar(time_s, close, close, close, close, close, volume, 1)


def history() -> list[Bar]:
    return [
        bar(DAY0 - 86_400, volume=10.0),  # bucket 0, previous day
        bar(DAY0 - 86_400 + 900, volume=5.0),  # bucket 1, previous day
        bar(DAY0 - 2 * 86_400, volume=30.0),  # bucket 0, two days ago
    ]


def test_vwap_weights_follow_time_of_day_profile() -> None:
    assert vwap_weights(history(), DAY0 * NS, 1800, 2) == (20.0, 5.0)


def test_vwap_weights_fill_empty_buckets_with_mean_profile() -> None:
    assert vwap_weights(history(), (DAY0 + 1800) * NS, 900, 1) == (12.5,)


def test_vwap_weights_short_horizon_is_flat() -> None:
    assert vwap_weights(history(), DAY0 * NS, 60, 6) == (20.0,) * 6


@pytest.mark.parametrize(
    ("bars", "start_ns", "duration_s", "slices"),
    [
        ([], DAY0 * NS, 60, 1),
        (history(), DAY0 * NS, 0, 1),
        (history(), DAY0 * NS, 60, 0),
        (history(), -1, 60, 1),
        ([bar(DAY0, volume=0.0)], DAY0 * NS, 60, 1),
    ],
    ids=["no-bars", "zero-duration", "zero-slices", "negative-start", "zero-volume"],
)
def test_vwap_weights_reject_bad_input(
    bars: list[Bar], start_ns: int, duration_s: int, slices: int
) -> None:
    with pytest.raises(CalibrationError):
        vwap_weights(bars, start_ns, duration_s, slices)


def test_sigma_from_one_minute_closes() -> None:
    bars = [bar(DAY0 + 60 * i, close=100.0 + (i % 2)) for i in range(61)]
    random.Random(7).shuffle(bars)  # noqa: S311 - deterministic ordering check, not crypto
    expected = math.sqrt(60 / 59) / math.sqrt(60)
    assert estimate_sigma(bars) == pytest.approx(expected)


def test_sigma_needs_enough_bars_and_movement() -> None:
    with pytest.raises(CalibrationError, match="at least"):
        estimate_sigma([bar(DAY0 + 60 * i) for i in range(10)])
    with pytest.raises(CalibrationError, match="volatility"):
        estimate_sigma([bar(DAY0 + 60 * i) for i in range(61)])


def test_eta_from_book_cost_curve() -> None:
    asks = [(100.0, 1.0), (101.0, 1.0), (102.0, 2.0)]
    # sizes 0.25, 0.5, 1, 2 cost 0, 0, 0, 0.5 above touch; slope = 0.53125/1.796875
    assert estimate_eta(asks, slice_qty=0.5, tau_s=10.0) == pytest.approx(0.53125 / 1.796875 * 10)


def test_eta_sell_side_uses_distance_below_touch() -> None:
    bids = [(100.0, 1.0), (99.0, 1.0), (98.0, 2.0)]
    assert estimate_eta(bids, slice_qty=0.5, tau_s=10.0) == pytest.approx(0.53125 / 1.796875 * 10)


def test_eta_rejects_thin_or_flat_books() -> None:
    with pytest.raises(CalibrationError, match="thin"):
        estimate_eta([(100.0, 0.5)], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError, match="slope"):
        estimate_eta([(100.0, 100.0)], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError):
        estimate_eta([], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError):
        estimate_eta([(100.0, 1.0)], slice_qty=0.0, tau_s=10.0)


def test_urgency_presets() -> None:
    assert URGENCY_RISK_AVERSION == {"low": 3e-6, "medium": 3e-5, "high": 3e-4}
