import json

import pytest

from slipstream.coinbase import (
    COINBASE_WS_URL,
    CoinbaseMessageError,
    CoinbaseStream,
    subscribe_messages,
)
from slipstream.models import BookUpdate, TradeBatch


def l2(seq: int, kind: str, updates: list[dict[str, str]], product: str = "BTC-USD") -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "client_id": "",
            "timestamp": "2026-09-24T00:00:00Z",
            "sequence_num": seq,
            "events": [{"type": kind, "product_id": product, "updates": updates}],
        }
    )


def upd(side: str, price: str, qty: str) -> dict[str, str]:
    return {
        "side": side,
        "event_time": "2026-09-24T00:00:00Z",
        "price_level": price,
        "new_quantity": qty,
    }


def trades(seq: int, kind: str, rows: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "channel": "market_trades",
            "client_id": "",
            "timestamp": "2026-09-24T00:00:00Z",
            "sequence_num": seq,
            "events": [{"type": kind, "trades": rows}],
        }
    )


def trade(price: str, size: str, product: str = "BTC-USD") -> dict[str, str]:
    return {
        "trade_id": "1",
        "product_id": product,
        "price": price,
        "size": size,
        "side": "BUY",
        "time": "2026-09-24T00:00:00Z",
    }


def test_endpoint_and_subscriptions() -> None:
    assert COINBASE_WS_URL == "wss://advanced-trade-ws.coinbase.com"
    assert [json.loads(m) for m in subscribe_messages("BTC/USD")] == [
        {"type": "subscribe", "channel": "level2", "product_ids": ["BTC-USD"]},
        {"type": "subscribe", "channel": "market_trades", "product_ids": ["BTC-USD"]},
        {"type": "subscribe", "channel": "heartbeats", "product_ids": ["BTC-USD"]},
    ]


def test_snapshot_is_sorted_truncated_and_tagged() -> None:
    stream = CoinbaseStream("BTC/USD", depth=2)
    raw = l2(
        0,
        "snapshot",
        [
            upd("bid", "99", "1"),
            upd("bid", "98", "2"),
            upd("bid", "99.5", "3"),
            upd("offer", "101", "1"),
            upd("offer", "100.5", "2"),
            upd("offer", "102", "0"),
        ],
    )
    assert stream.parse(raw) == BookUpdate(
        "BTC/USD", True, ((99.5, 3.0), (99.0, 1.0)), ((100.5, 2.0), (101.0, 1.0)), "coinbase"
    )


def test_update_passes_deltas_through() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(0, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    assert stream.parse(l2(1, "update", [upd("bid", "99", "0"), upd("offer", "100.9", "4")])) == (
        BookUpdate("BTC/USD", False, ((99.0, 0.0),), ((100.9, 4.0),), "coinbase")
    )


def test_trades_update_and_snapshot() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    assert stream.parse(trades(0, "snapshot", [trade("100", "1")])) is None
    assert stream.parse(trades(1, "update", [trade("100.5", "0.2"), trade("100.4", "0.3")])) == (
        TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3)), "coinbase")
    )


def test_heartbeats_and_subscriptions_are_ignored() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    heartbeat = {
        "channel": "heartbeats",
        "client_id": "",
        "timestamp": "t",
        "sequence_num": 0,
        "events": [{"current_time": "t", "heartbeat_counter": 1}],
    }
    subs = {
        "channel": "subscriptions",
        "client_id": "",
        "timestamp": "t",
        "sequence_num": 1,
        "events": [{"subscriptions": {"level2": ["BTC-USD"]}}],
    }
    assert stream.parse(json.dumps(heartbeat)) is None
    assert stream.parse(json.dumps(subs)) is None


def test_sequence_gap_raises() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(5, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    with pytest.raises(CoinbaseMessageError, match="sequence gap"):
        stream.parse(l2(7, "update", [upd("bid", "99", "2")]))


def test_error_message_raises() -> None:
    with pytest.raises(CoinbaseMessageError, match="coinbase error"):
        CoinbaseStream("BTC/USD", 10).parse(json.dumps({"type": "error", "message": "bad"}))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[" * 100_000,
        "1" * 5000,
        json.dumps([1]),
        l2(0, "snapshot", [upd("bid", "99", "1")], product="ETH-USD"),
        l2(0, "weird", []),
        l2(0, "update", [upd("middle", "99", "1")]),
        l2(0, "update", [upd("bid", "-1", "1")]),
        l2(0, "update", [upd("bid", "99", "-1")]),
        l2(0, "update", [upd("bid", "nan", "1")]),
        l2(0, "update", [upd("bid", "1e400", "1")]),
        l2(0, "update", [upd("bid", "1" * 65, "1")]),
        l2(0, "update", [{"side": "bid", "price_level": 99, "new_quantity": "1"}]),
        json.dumps({"channel": "l2_data", "sequence_num": True, "events": []}),
        json.dumps({"channel": "l2_data", "sequence_num": 0, "events": "x"}),
        trades(0, "update", [trade("100", "0")]),
        trades(0, "update", [trade("100", "1", product="ETH-USD")]),
        json.dumps({"channel": "l2_data", "sequence_num": 0, "events": [{"type": []}]}),
        l2(0, "update", [{"side": [], "price_level": "1", "new_quantity": "1"}]),
        l2(0, "update", [{"side": {"a": 1}, "price_level": "1", "new_quantity": "1"}]),
    ],
    ids=[
        "not-json",
        "deep",
        "huge-int",
        "not-object",
        "wrong-product",
        "bad-type",
        "bad-side",
        "neg-price",
        "neg-qty",
        "nan",
        "overflow",
        "long",
        "number-not-string",
        "bool-seq",
        "events-not-list",
        "zero-trade",
        "trade-wrong-product",
        "unhashable-event-type",
        "unhashable-side-list",
        "unhashable-side-dict",
    ],
)
def test_hostile_input_only_raises_coinbase_error(raw: str) -> None:
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("BTC/USD", depth=10).parse(raw)


def test_rejects_unknown_symbol_and_bad_depth() -> None:
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("DOGE/XYZ", depth=10)
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("BTC/USD", depth=0)
