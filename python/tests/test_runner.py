import json
import logging

import pytest
from conftest import FakeEngine

from slipstream.calibration import CalibrationData, CalibrationError
from slipstream.coinbase import CoinbaseMessageError
from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import Bar
from slipstream.models import (
    BookUpdate,
    Fill,
    MarketDataError,
    OrderSpec,
    PovParams,
    ScheduleParams,
    TwapParams,
)
from slipstream.runner import ExecutionRunner, OrderRejectedError
from slipstream.v1 import execution_pb2 as pb

SPEC = OrderSpec("o-1", "buy", 1.0, 4, 4)


def snapshot(symbol: str = "BTC/USD") -> str:
    return json.dumps(
        {
            "channel": "book",
            "type": "snapshot",
            "data": [
                {
                    "symbol": symbol,
                    "bids": [{"price": 99.0, "qty": 1.0}],
                    "asks": [{"price": 101.0, "qty": 1.0}],
                }
            ],
        }
    )


HEARTBEAT = json.dumps({"channel": "heartbeat"})


def make_runner(engine: FakeEngine) -> ExecutionRunner:
    return ExecutionRunner(engine, SPEC, "BTC/USD", logging.getLogger("test"))


def test_does_nothing_before_first_snapshot(fake_engine: FakeEngine) -> None:
    make_runner(fake_engine).on_message(HEARTBEAT, 1)
    assert fake_engine.submits == []
    assert fake_engine.steps == []


def test_submits_on_first_snapshot_then_steps(fake_engine: FakeEngine) -> None:
    make_runner(fake_engine).on_message(snapshot(), 100)
    assert len(fake_engine.books) == 1
    assert fake_engine.submits == [(SPEC, 100, TwapParams())]
    assert fake_engine.steps == [100]


def test_steps_on_every_later_message_without_resubmitting(fake_engine: FakeEngine) -> None:
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 100)
    runner.on_message(HEARTBEAT, 200)
    runner.on_message(snapshot(), 300)
    assert len(fake_engine.submits) == 1
    assert fake_engine.steps == [100, 200, 300]


def test_rejected_order_raises(fake_engine: FakeEngine) -> None:
    fake_engine.accept = False
    fake_engine.reason = "position limit exceeded"
    with pytest.raises(OrderRejectedError, match="position limit"):
        make_runner(fake_engine).on_message(snapshot(), 1)


def test_symbol_mismatch_raises(fake_engine: FakeEngine) -> None:
    with pytest.raises(MarketDataError, match="symbol"):
        make_runner(fake_engine).on_message(snapshot("ETH/USD"), 1)


def test_records_fills_and_reports_done(fake_engine: FakeEngine) -> None:
    fill = Fill("o-1", 100, 1.0, 101.0)
    fake_engine.fills_per_step = [[fill]]
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 100)
    assert runner.fills == [fill]
    assert not runner.is_done()
    fake_engine.state = pb.ORDER_STATE_COMPLETED
    runner.on_message(HEARTBEAT, 200)
    assert runner.is_done()


def test_is_done_reflects_the_last_step_without_calling_get_status(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_engine.done_after_steps = 1
    calls = 0
    orig_status = fake_engine.status

    def spy_status() -> pb.StatusReply:
        nonlocal calls
        calls += 1
        return orig_status()

    monkeypatch.setattr(fake_engine, "status", spy_status)
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 100)
    assert runner.is_done()
    assert calls == 0


def trade(msg_type: str = "update", symbol: str = "BTC/USD") -> str:
    return json.dumps(
        {
            "channel": "trade",
            "type": msg_type,
            "data": [{"symbol": symbol, "price": 100.0, "qty": 1.0}],
        }
    )


def test_trade_updates_are_forwarded_but_snapshots_ignored(fake_engine: FakeEngine) -> None:
    runner = make_runner(fake_engine)
    runner.on_message(trade("snapshot"), 1)
    runner.on_message(trade(), 2)
    assert [batch.is_snapshot for batch in fake_engine.trades] == [False]
    assert fake_engine.books == []
    assert fake_engine.submits == []


def test_trade_symbol_mismatch_raises(fake_engine: FakeEngine) -> None:
    with pytest.raises(MarketDataError, match="symbol"):
        make_runner(fake_engine).on_message(trade(symbol="ETH/USD"), 1)


def test_submits_every_spec_with_calibrated_params(fake_engine: FakeEngine) -> None:
    specs = [
        OrderSpec("a", "buy", 1.0, 4, 4),
        OrderSpec("b", "buy", 1.0, 4, 4, algo="pov", participation=0.3),
    ]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"))
    runner.on_message(snapshot(), 100)
    assert [(s.order_id, p) for s, _, p in fake_engine.submits] == [
        ("a", TwapParams()),
        ("b", PovParams(0.3)),
    ]
    assert [status.order_id for status in runner.order_statuses()] == ["a", "b"]


def test_done_only_when_every_order_is_terminal(fake_engine: FakeEngine) -> None:
    specs = [OrderSpec("a", "buy", 1.0, 4, 4), OrderSpec("b", "buy", 1.0, 4, 4)]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"))
    assert not runner.is_done()
    runner.on_message(snapshot(), 100)
    assert not runner.is_done()
    fake_engine.state = pb.ORDER_STATE_COMPLETED
    runner.on_message(HEARTBEAT, 200)
    assert runner.is_done()


def test_working_orders_is_set_right_after_submit_before_any_step(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for the D1 ordering requirement: even if step() were never called,
    # a freshly submitted runner must not report done.
    monkeypatch.setattr(fake_engine, "step", lambda now_ns: pytest.fail("step should not run"))
    runner = ExecutionRunner(
        fake_engine, [OrderSpec("a", "buy", 1.0, 4, 4)], "BTC/USD", logging.getLogger("test")
    )
    runner._submit_all(100)  # exercising the ordering guarantee directly, before any step
    assert runner._working == 1
    assert not runner.is_done()


def test_calibrated_algo_without_data_fails_before_submitting(fake_engine: FakeEngine) -> None:
    spec = OrderSpec("v", "buy", 1.0, 4, 4, algo="vwap")
    runner = ExecutionRunner(fake_engine, spec, "BTC/USD", logging.getLogger("test"))
    with pytest.raises(CalibrationError):
        runner.on_message(snapshot(), 100)
    assert fake_engine.submits == []


def test_rejects_only_the_failing_order_but_leaves_earlier_ones_submitted(
    fake_engine: FakeEngine,
) -> None:
    # Documented limitation: "a" was accepted by the engine before "b" was rejected, and
    # stays working there. The runner does not attempt to cancel it.
    fake_engine.reject_ids = {"b"}
    specs = [OrderSpec("a", "buy", 1.0, 4, 4), OrderSpec("b", "buy", 1.0, 4, 4)]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"))
    with pytest.raises(OrderRejectedError, match="order b rejected"):
        runner.on_message(snapshot(), 100)
    assert [s.order_id for s, _, _ in fake_engine.submits] == ["a", "b"]


def _bars_1m(count: int) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            time_s=i * 60,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0 + (0.05 if i % 2 == 0 else -0.05) + 0.01 * i,
            vwap=100.0,
            volume=1.0,
            count=1,
        )
        for i in range(count)
    )


def _bars_15m(count: int) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            time_s=i * 900,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            vwap=100.0,
            volume=10.0,
            count=5,
        )
        for i in range(count)
    )


def test_calibrates_every_spec_before_submitting_any(fake_engine: FakeEngine) -> None:
    calibration = CalibrationData(_bars_15m(4), _bars_1m(61))
    specs = [
        OrderSpec("v", "buy", 1.0, 4, 4, algo="twap"),
        OrderSpec("ac", "buy", 1.0, 4, 4, algo="almgren_chriss"),
    ]
    runner = ExecutionRunner(fake_engine, specs, "BTC/USD", logging.getLogger("test"), calibration)
    # snapshot() has only 1 ask level, too thin to calibrate Almgren-Chriss impact.
    with pytest.raises(CalibrationError):
        runner.on_message(snapshot(), 100)
    assert fake_engine.submits == []


def cb_snapshot(seq: int = 0) -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "sequence_num": seq,
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "99", "new_quantity": "1"},
                        {"side": "offer", "price_level": "101", "new_quantity": "1"},
                    ],
                }
            ],
        }
    )


def test_waits_for_a_snapshot_from_every_venue(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("kraken", "coinbase")
    )
    runner.on_message(snapshot(), 100, "kraken")
    assert fake_engine.submits == []
    runner.on_message(cb_snapshot(), 200, "coinbase")
    assert [s.order_id for s, _, _ in fake_engine.submits] == ["o-1"]
    assert [book.venue for book in fake_engine.books] == ["kraken", "coinbase"]


def test_messages_from_unconfigured_venue_are_rejected(fake_engine: FakeEngine) -> None:
    with pytest.raises(ValueError, match="venue"):
        make_runner(fake_engine).on_message(cb_snapshot(), 1, "coinbase")


def test_venue_errors_share_a_market_data_base() -> None:
    assert issubclass(KrakenMessageError, MarketDataError)
    assert issubclass(CoinbaseMessageError, MarketDataError)


def test_trades_flow_while_waiting_for_a_late_venue(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("kraken", "coinbase")
    )
    runner.on_message(snapshot(), 100, "kraken")
    runner.on_message(trade(), 150, "kraken")
    assert len(fake_engine.trades) == 1
    assert fake_engine.submits == []
    assert fake_engine.steps == []
    runner.on_message(cb_snapshot(), 300, "coinbase")
    assert [(s.order_id, start) for s, start, _ in fake_engine.submits] == [("o-1", 300)]
    assert fake_engine.steps == [300]


def test_book_updates_carry_their_receive_time(fake_engine: FakeEngine) -> None:
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 1234)
    assert fake_engine.book_recv_ns == [1234]


def test_calibration_uses_the_consolidated_fee_adjusted_book(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[BookUpdate] = []

    def spy(
        spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
    ) -> ScheduleParams:
        seen.append(book)
        return TwapParams()

    monkeypatch.setattr("slipstream.runner.schedule_params", spy)
    runner = ExecutionRunner(
        fake_engine,
        SPEC,
        "BTC/USD",
        logging.getLogger("t"),
        venues=("kraken", "coinbase"),
        fee_bps={"kraken": 0.0, "coinbase": 100.0},
    )
    runner.on_message(snapshot(), 100, "kraken")
    runner.on_message(cb_snapshot(), 200, "coinbase")
    (book,) = seen
    # pytest.approx() on this pytest version does not support nesting past one level, so the
    # per-price tolerance is applied by hand instead of wrapping the whole tuple of tuples.
    assert book.asks == ((pytest.approx(101.0), 1.0), (pytest.approx(102.01), 1.0))
    assert book.bids == ((pytest.approx(99.0), 1.0), (pytest.approx(98.01), 1.0))


def test_calibration_book_includes_deltas_since_the_first_snapshot(
    fake_engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[BookUpdate] = []

    def spy(
        spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
    ) -> ScheduleParams:
        seen.append(book)
        return TwapParams()

    monkeypatch.setattr("slipstream.runner.schedule_params", spy)
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("kraken", "coinbase")
    )
    runner.on_message(snapshot(), 100, "kraken")
    delta = {
        "channel": "book",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "bids": [], "asks": [{"price": 100.5, "qty": 2.0}]}],
    }
    runner.on_message(json.dumps(delta), 150, "kraken")
    runner.on_message(cb_snapshot(), 200, "coinbase")
    (book,) = seen
    assert book.asks == ((100.5, 2.0), (101.0, 1.0), (101.0, 1.0))


def test_fill_log_carries_venue_and_fee(
    fake_engine: FakeEngine, caplog: pytest.LogCaptureFixture
) -> None:
    fake_engine.fills_per_step = [[Fill("o-1", 5, 0.25, 101.0, "coinbase", 0.02)]]
    runner = make_runner(fake_engine)
    with caplog.at_level(logging.INFO):
        runner.on_message(snapshot(), 5)
    fields = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "fill"]
    assert fields[0]["venue"] == "coinbase"
    assert fields[0]["fee"] == 0.02


def test_runner_exposes_its_venues(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(
        fake_engine, SPEC, "BTC/USD", logging.getLogger("t"), venues=("coinbase", "kraken")
    )
    assert runner.venues == frozenset({"kraken", "coinbase"})
