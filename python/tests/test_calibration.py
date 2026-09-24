import math
import random

import pytest

from slipstream.calibration import (
    URGENCY_RISK_AVERSION,
    CalibrationData,
    CalibrationError,
    estimate_eta,
    estimate_sigma,
    schedule_params,
    vwap_weights,
)
from slipstream.kraken_rest import Bar
from slipstream.models import (
    AlmgrenChrissParams,
    BookUpdate,
    OrderSpec,
    PovParams,
    TwapParams,
    VwapParams,
)

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
    # cumulative (Q, cost-above-touch): (1, 0), (2, 0.5), (4, 1.25)
    # least-squares slope = (23/12) / (14/3) = 23/56 = 0.4107142857142857
    assert estimate_eta(asks, tau_s=10.0) == pytest.approx(23 / 56 * 10)


def test_eta_sell_side_uses_distance_below_touch() -> None:
    bids = [(100.0, 1.0), (99.0, 1.0), (98.0, 2.0)]
    # same cumulative (Q, cost) curve as the ask side -> same slope
    assert estimate_eta(bids, tau_s=10.0) == pytest.approx(23 / 56 * 10)


def test_eta_regression_deep_touch_still_finite_and_positive() -> None:
    # Reproduces the live-run bug: the first level alone dwarfs any realistic
    # slice, so slice-multiple probing measured zero impact. The depth-curve
    # estimator still finds a positive slope from the visible levels.
    asks = [(84150.0, 3.0), (84150.5, 2.0), (84151.0, 4.0), (84152.0, 5.0)]
    eta = estimate_eta(asks, tau_s=60.0)
    assert math.isfinite(eta)
    assert eta > 0
    assert eta == pytest.approx(5.8014470806457235)


def test_eta_rejects_thin_or_flat_books() -> None:
    with pytest.raises(CalibrationError, match="thin"):
        estimate_eta([(100.0, 0.5), (101.0, 0.5)], tau_s=10.0)
    with pytest.raises(CalibrationError, match="slope"):
        estimate_eta([(100.0, 1.0), (100.0, 1.0), (100.0, 1.0)], tau_s=10.0)
    with pytest.raises(CalibrationError):
        estimate_eta([], tau_s=10.0)


@pytest.mark.parametrize(
    ("liquidity", "tau_s"),
    [
        ([(100.0, 1.0), (float("nan"), 1.0), (102.0, 1.0)], 10.0),
        ([(100.0, 1.0), (0.0, 1.0), (102.0, 1.0)], 10.0),
        ([(100.0, 1.0), (101.0, 0.0), (102.0, 1.0)], 10.0),
        ([(100.0, 1.0), (101.0, 1.0), (102.0, 1.0)], 0.0),
        ([(100.0, 1.0), (101.0, 1.0), (102.0, 1.0)], float("nan")),
    ],
    ids=["nan-price", "zero-price", "zero-qty", "zero-tau", "nan-tau"],
)
def test_eta_rejects_invalid_liquidity_or_tau(
    liquidity: list[tuple[float, float]], tau_s: float
) -> None:
    with pytest.raises(CalibrationError):
        estimate_eta(liquidity, tau_s=tau_s)


def test_urgency_presets() -> None:
    assert URGENCY_RISK_AVERSION == {"low": 3e-6, "medium": 3e-5, "high": 3e-4}


BOOK = BookUpdate(
    "BTC/USD",
    True,
    ((99.0, 1.0), (98.0, 1.0), (97.0, 2.0)),
    ((102.0, 2.0), (100.0, 1.0), (101.0, 1.0)),
)
ONE_MINUTE = tuple(bar(DAY0 + 60 * i, close=100.0 + (i % 2)) for i in range(61))
DATA = CalibrationData(bars_15m=tuple(history()), bars_1m=ONE_MINUTE)


def spec(algo: str, **kwargs: object) -> OrderSpec:
    return OrderSpec("o-1", "buy", 2.0, 4, 4, algo=algo, **kwargs)  # type: ignore[arg-type]


def test_twap_and_pov_need_no_calibration_data() -> None:
    assert schedule_params(spec("twap"), BOOK, DAY0 * NS, None) == TwapParams()
    assert schedule_params(spec("pov", participation=0.2), BOOK, DAY0 * NS, None) == PovParams(0.2)


def test_vwap_uses_time_of_day_weights() -> None:
    params = schedule_params(spec("vwap"), BOOK, DAY0 * NS, DATA)
    assert params == VwapParams((20.0, 20.0, 20.0, 20.0))


def test_almgren_chriss_calibrates_sigma_eta_and_urgency() -> None:
    params = schedule_params(spec("almgren_chriss", urgency="high"), BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.sigma == pytest.approx(math.sqrt(60 / 59) / math.sqrt(60))
    # asks re-sorted best-first; tau 1 s -> same depth curve as test_eta_from_book_cost_curve
    assert params.eta == pytest.approx(23 / 56 * 1.0)
    assert params.risk_aversion == 3e-4


def test_explicit_risk_aversion_overrides_urgency() -> None:
    params = schedule_params(spec("almgren_chriss", risk_aversion=7.5), BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.risk_aversion == 7.5


def test_sell_side_calibrates_against_bids() -> None:
    sell = OrderSpec("o-1", "sell", 2.0, 4, 4, algo="almgren_chriss")
    params = schedule_params(sell, BOOK, DAY0 * NS, DATA)
    assert isinstance(params, AlmgrenChrissParams)
    assert params.eta == pytest.approx(23 / 56 * 1.0)


@pytest.mark.parametrize("algo", ["vwap", "almgren_chriss"])
def test_calibrated_algos_require_data(algo: str) -> None:
    with pytest.raises(CalibrationError, match="calibration data"):
        schedule_params(spec(algo), BOOK, DAY0 * NS, None)
