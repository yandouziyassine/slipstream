import pytest

from slipstream.config import ConfigError
from slipstream.engine_client import EngineClient, EngineError, book_update_to_proto, order_to_proto
from slipstream.models import BookUpdate, OrderSpec
from slipstream.v1 import execution_pb2 as pb


def test_book_update_to_proto() -> None:
    msg = book_update_to_proto(BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),)))
    assert msg.symbol == "BTC/USD"
    assert msg.is_snapshot
    assert [(level.price, level.qty) for level in msg.bids] == [(99.0, 1.0)]
    assert [(level.price, level.qty) for level in msg.asks] == [(101.0, 2.0)]


def test_order_to_proto_converts_seconds_to_nanoseconds() -> None:
    msg = order_to_proto(OrderSpec("o-1", "sell", 0.5, 60, 6), start_ns=123)
    assert msg.order_id == "o-1"
    assert msg.side == pb.SIDE_SELL
    assert msg.start_ns == 123
    assert msg.duration_ns == 60_000_000_000
    assert msg.num_slices == 6


def test_client_rejects_non_loopback_address() -> None:
    with pytest.raises(ConfigError):
        EngineClient("10.0.0.1:50051")


def test_unreachable_engine_raises_engine_error() -> None:
    client = EngineClient("127.0.0.1:1", timeout_s=0.5)
    try:
        with pytest.raises(EngineError):
            client.step(0)
    finally:
        client.close()


def test_wait_ready_times_out_when_engine_absent() -> None:
    client = EngineClient("127.0.0.1:1")
    try:
        with pytest.raises(EngineError, match="not reachable"):
            client.wait_ready(timeout_s=0.5)
    finally:
        client.close()
