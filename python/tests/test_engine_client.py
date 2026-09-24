import pytest

from slipstream.config import ConfigError
from slipstream.engine_client import (
    EngineClient,
    EngineError,
    book_update_to_proto,
    order_to_proto,
    trade_batch_to_proto,
)
from slipstream.models import (
    AlmgrenChrissParams,
    BookUpdate,
    OrderSpec,
    PovParams,
    TradeBatch,
    TwapParams,
    VwapParams,
)
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


def test_order_defaults_to_twap_schedule() -> None:
    msg = order_to_proto(OrderSpec("o-1", "buy", 1.0, 4, 4), start_ns=0)
    assert msg.WhichOneof("schedule") == "twap"


def test_order_carries_schedule_params() -> None:
    spec = OrderSpec("o-1", "buy", 1.0, 4, 2)
    vwap = order_to_proto(spec, 0, VwapParams((1.0, 3.0)))
    assert vwap.WhichOneof("schedule") == "vwap"
    assert list(vwap.vwap.weights) == [1.0, 3.0]
    ac = order_to_proto(spec, 0, AlmgrenChrissParams(0.5, 2.0, 3e-5))
    assert ac.WhichOneof("schedule") == "almgren_chriss"
    assert (ac.almgren_chriss.sigma, ac.almgren_chriss.eta, ac.almgren_chriss.risk_aversion) == (
        0.5,
        2.0,
        3e-5,
    )
    pov = order_to_proto(spec, 0, PovParams(0.25))
    assert pov.WhichOneof("schedule") == "pov"
    assert pov.pov.participation == 0.25
    assert order_to_proto(spec, 0, TwapParams()).WhichOneof("schedule") == "twap"


def test_trade_batch_to_proto() -> None:
    msg = trade_batch_to_proto(TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3))))
    assert msg.symbol == "BTC/USD"
    assert [(t.price, t.qty) for t in msg.trades] == [(100.5, 0.2), (100.4, 0.3)]
