import json
from typing import Any

import pytest

from slipstream.kraken import (
    KrakenMessageError,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.models import TradeBatch


def book_msg(
    msg_type: str = "snapshot",
    bids: Any = None,
    asks: Any = None,
    symbol: str = "BTC/USD",
) -> str:
    return json.dumps(
        {
            "channel": "book",
            "type": msg_type,
            "data": [
                {
                    "symbol": symbol,
                    "bids": bids if bids is not None else [{"price": 99.5, "qty": 1.0}],
                    "asks": asks if asks is not None else [{"price": 100.5, "qty": 2.0}],
                    "checksum": 123,
                }
            ],
        }
    )


def test_parses_snapshot() -> None:
    update = parse_message(book_msg())
    assert update is not None
    assert update.symbol == "BTC/USD"
    assert update.is_snapshot
    assert update.bids == ((99.5, 1.0),)
    assert update.asks == ((100.5, 2.0),)


def test_parses_update_with_zero_qty_removal() -> None:
    update = parse_message(book_msg("update", bids=[{"price": 99.5, "qty": 0}], asks=[]))
    assert update is not None
    assert not update.is_snapshot
    assert update.bids == ((99.5, 0.0),)
    assert update.asks == ()


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"channel": "heartbeat"}),
        json.dumps({"channel": "status", "type": "update", "data": []}),
        json.dumps({"method": "subscribe", "success": True, "result": {}}),
    ],
)
def test_non_book_messages_are_ignored(raw: str) -> None:
    assert parse_message(raw) is None


def test_failed_subscription_raises() -> None:
    raw = json.dumps(
        {"method": "subscribe", "success": False, "error": "Currency pair not supported"}
    )
    with pytest.raises(KrakenMessageError, match="subscription failed"):
        parse_message(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"channel": "book", "type": "weird", "data": []}),
        json.dumps({"channel": "book", "type": "update", "data": []}),
        book_msg(bids=[{"price": -1, "qty": 1}]),
        book_msg(bids=[{"price": 1, "qty": -1}]),
        book_msg(bids=[{"price": "1", "qty": 1}]),
        book_msg(bids=[{"price": True, "qty": 1}]),
        book_msg(bids=[{"qty": 1}]),
        book_msg(asks="nope"),
        book_msg(symbol=""),
        '{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USD",'
        '"bids":[{"price":NaN,"qty":1}],"asks":[]}]}',
    ],
)
def test_malformed_book_messages_raise(raw: str) -> None:
    with pytest.raises(KrakenMessageError):
        parse_message(raw)


def test_rejects_too_many_levels() -> None:
    levels = [{"price": 1.0 + i, "qty": 1.0} for i in range(1001)]
    with pytest.raises(KrakenMessageError, match="too many"):
        parse_message(book_msg(bids=levels))


def test_rejects_oversized_message() -> None:
    with pytest.raises(KrakenMessageError, match="too large"):
        parse_message(" " * ((1 << 20) + 1))


def test_subscribe_message() -> None:
    assert json.loads(subscribe_message("BTC/USD", 10)) == {
        "method": "subscribe",
        "params": {"channel": "book", "symbol": ["BTC/USD"], "depth": 10, "snapshot": True},
    }


HUGE_INT = "1" + "0" * 400


@pytest.mark.parametrize(
    "raw",
    [
        "[" * 100_000,
        "1" * 5000,
        b"\xff\xfe{",
        '{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USD",'
        f'"bids":[{{"price":{HUGE_INT},"qty":1}}],"asks":[]}}]}}',
        '{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USD",'
        f'"bids":[{{"price":1,"qty":{HUGE_INT}}}],"asks":[]}}]}}',
    ],
    ids=[
        "deeply_nested",
        "huge_int_literal",
        "invalid_utf8",
        "huge_price_digits",
        "huge_qty_digits",
    ],
)
def test_hostile_input_only_raises_kraken_error(raw: str | bytes) -> None:
    with pytest.raises(KrakenMessageError):
        parse_message(raw)


def trade_msg(msg_type: str = "update", data: Any = None) -> str:
    rows = (
        data
        if data is not None
        else [
            {
                "symbol": "BTC/USD",
                "side": "buy",
                "price": 100.5,
                "qty": 0.2,
                "ord_type": "market",
                "trade_id": 1,
                "timestamp": "2026-09-24T00:00:00.000000Z",
            },
            {
                "symbol": "BTC/USD",
                "side": "sell",
                "price": 100.4,
                "qty": 0.3,
                "ord_type": "limit",
                "trade_id": 2,
                "timestamp": "2026-09-24T00:00:00.100000Z",
            },
        ]
    )
    return json.dumps({"channel": "trade", "type": msg_type, "data": rows})


def test_parses_trade_update() -> None:
    assert parse_message(trade_msg()) == TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3)))


def test_trade_snapshot_is_flagged() -> None:
    batch = parse_message(trade_msg("snapshot"))
    assert isinstance(batch, TradeBatch)
    assert batch.is_snapshot


@pytest.mark.parametrize(
    "raw",
    [
        trade_msg(data=[]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": 100.0, "qty": 0}]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": -1, "qty": 1}]),
        trade_msg(
            data=[
                {"symbol": "BTC/USD", "price": 1, "qty": 1},
                {"symbol": "ETH/USD", "price": 1, "qty": 1},
            ]
        ),
        trade_msg(data=[{"symbol": "BTC/USD", "price": 1, "qty": 1}] * 1001),
        trade_msg("weird"),
        json.dumps({"channel": "trade", "type": "update", "data": "x"}),
    ],
    ids=[
        "empty",
        "zero-qty",
        "negative-price",
        "mixed-symbols",
        "too-many",
        "bad-type",
        "data-not-list",
    ],
)
def test_malformed_trade_messages_raise(raw: str) -> None:
    with pytest.raises(KrakenMessageError):
        parse_message(raw)


def test_subscribe_trades_message_disables_snapshot() -> None:
    assert json.loads(subscribe_trades_message("BTC/USD")) == {
        "method": "subscribe",
        "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False},
    }
