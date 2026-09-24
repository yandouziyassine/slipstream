import json
import logging

import pytest
from conftest import FakeEngine

from slipstream.kraken import KrakenMessageError
from slipstream.models import Fill, OrderSpec
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
    assert fake_engine.submits == [(SPEC, 100)]
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
    with pytest.raises(KrakenMessageError, match="symbol"):
        make_runner(fake_engine).on_message(snapshot("ETH/USD"), 1)


def test_records_fills_and_reports_done(fake_engine: FakeEngine) -> None:
    fill = Fill("o-1", 100, 1.0, 101.0)
    fake_engine.fills_per_step = [[fill]]
    runner = make_runner(fake_engine)
    runner.on_message(snapshot(), 100)
    assert runner.fills == [fill]
    assert not runner.is_done()
    fake_engine.state = pb.ORDER_STATE_COMPLETED
    assert runner.is_done()


def test_trade_messages_do_not_touch_book_or_submit(fake_engine: FakeEngine) -> None:
    trade = json.dumps(
        {
            "channel": "trade",
            "type": "update",
            "data": [{"symbol": "BTC/USD", "price": 100.0, "qty": 1.0}],
        }
    )
    make_runner(fake_engine).on_message(trade, 1)
    assert fake_engine.books == []
    assert fake_engine.submits == []
