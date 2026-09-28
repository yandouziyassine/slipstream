import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from slipstream.kraken import (
    KrakenMessageError,
    KrakenStream,
    book_subscription,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken_checksum import BookChecksumError, book_checksum
from slipstream.models import BookUpdate, MarketDataError, TradeBatch


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
        '{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USD",'
        '"bids":[{"price":1e-400,"qty":1}],"asks":[]}]}',
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


# The worked example from https://docs.kraken.com/api/docs/guides/spot-ws-book-v2, spelled the
# way the live feed sends it: JSON numbers that keep the pair's full precision.
DOC_SNAPSHOT = (
    '{"channel":"book","type":"snapshot","data":[{"symbol":"BTC/USD","bids":['
    '{"price":45283.5,"qty":0.10000000},{"price":45283.4,"qty":1.54582015},'
    '{"price":45282.1,"qty":0.10000000},{"price":45281.0,"qty":0.10000000},'
    '{"price":45280.3,"qty":1.54592586},{"price":45279.0,"qty":0.07990000},'
    '{"price":45277.6,"qty":0.03310103},{"price":45277.5,"qty":0.30000000},'
    '{"price":45277.3,"qty":1.54602737},{"price":45276.6,"qty":0.15445238}],"asks":['
    '{"price":45285.2,"qty":0.00100000},{"price":45286.4,"qty":1.54571953},'
    '{"price":45286.6,"qty":1.54571109},{"price":45289.6,"qty":1.54560911},'
    '{"price":45290.2,"qty":0.15890660},{"price":45291.8,"qty":1.54553491},'
    '{"price":45294.7,"qty":0.04454749},{"price":45296.1,"qty":0.35380000},'
    '{"price":45297.5,"qty":0.09945542},{"price":45299.5,"qty":0.18772827}],'
    '"checksum":3310070434}]}'
)
DOC_CHECKSUM = 3310070434


def doc_update(bids: str, asks: str, checksum: int) -> str:
    return (
        '{"channel":"book","type":"update","data":[{"symbol":"BTC/USD",'
        f'"bids":[{bids}],"asks":[{asks}],"checksum":{checksum},'
        '"timestamp":"2023-10-06T17:35:55.440295Z"}]}'
    )


def doc_levels() -> tuple[list[tuple[Decimal, Decimal]], ...]:
    msg = json.loads(DOC_SNAPSHOT, parse_float=Decimal)["data"][0]
    return tuple([(lvl["price"], lvl["qty"]) for lvl in msg[side]] for side in ("asks", "bids"))


def test_stream_accepts_the_documented_snapshot() -> None:
    update = KrakenStream("BTC/USD", 10).parse(DOC_SNAPSHOT)
    assert isinstance(update, BookUpdate)
    assert update.is_snapshot
    assert update.bids[0] == (45283.5, 0.1)
    assert update.asks[0] == (45285.2, 0.001)


def test_stream_verifies_updates_after_the_snapshot() -> None:
    stream = KrakenStream("BTC/USD", 10)
    stream.parse(DOC_SNAPSHOT)
    asks, bids = doc_levels()
    asks[0] = (Decimal("45285.2"), Decimal("0.00200000"))
    bids.pop(1)
    checksum = book_checksum(asks, bids)
    update = stream.parse(
        doc_update(
            '{"price":45283.4,"qty":0.00000000}', '{"price":45285.2,"qty":0.00200000}', checksum
        )
    )
    assert isinstance(update, BookUpdate)
    assert update.bids == ((45283.4, 0.0),)
    assert update.asks == ((45285.2, 0.002),)


def test_stream_rejects_a_flipped_digit() -> None:
    corrupted = DOC_SNAPSHOT.replace('"qty":0.00100000', '"qty":0.00100001')
    with pytest.raises(BookChecksumError, match=r"kraken BTC/USD book checksum mismatch"):
        KrakenStream("BTC/USD", 10).parse(corrupted)


def test_stream_rejects_a_wrong_checksum_on_an_update() -> None:
    stream = KrakenStream("BTC/USD", 10)
    stream.parse(DOC_SNAPSHOT)
    with pytest.raises(BookChecksumError, match="mismatch"):
        stream.parse(doc_update('{"price":45283.4,"qty":0.00000000}', "", DOC_CHECKSUM))


def test_stream_checksums_the_wire_text_not_a_float_round_trip() -> None:
    lossy = json.dumps(json.loads(DOC_SNAPSHOT))
    with pytest.raises(BookChecksumError, match="mismatch"):
        KrakenStream("BTC/USD", 10).parse(lossy)


def test_mismatch_is_a_market_data_error() -> None:
    assert issubclass(BookChecksumError, MarketDataError)


@pytest.mark.parametrize(
    "checksum",
    ['"3310070434"', "true", "-1", str(1 << 32), "3310070434.0", "null"],
    ids=["string", "bool", "negative", "too-big", "float", "null"],
)
def test_stream_rejects_an_invalid_checksum_field(checksum: str) -> None:
    raw = DOC_SNAPSHOT.replace('"checksum":3310070434', f'"checksum":{checksum}')
    with pytest.raises(KrakenMessageError, match="checksum"):
        KrakenStream("BTC/USD", 10).parse(raw)


def test_stream_requires_a_checksum() -> None:
    raw = DOC_SNAPSHOT.replace(',"checksum":3310070434', "")
    with pytest.raises(KrakenMessageError, match="missing checksum"):
        KrakenStream("BTC/USD", 10).parse(raw)


def test_stream_rejects_an_update_before_the_snapshot() -> None:
    with pytest.raises(KrakenMessageError, match="before the snapshot"):
        KrakenStream("BTC/USD", 10).parse(doc_update("", "", 0))


def test_stream_rejects_another_symbol() -> None:
    with pytest.raises(KrakenMessageError, match="unexpected symbol"):
        KrakenStream("ETH/USD", 10).parse(DOC_SNAPSHOT)


def test_stream_rejects_an_unsupported_depth() -> None:
    with pytest.raises(ValueError, match="depth"):
        KrakenStream("BTC/USD", 20)


def test_stream_passes_trades_and_heartbeats_through() -> None:
    stream = KrakenStream("BTC/USD", 10)
    assert stream.parse(json.dumps({"channel": "heartbeat"})) is None
    assert stream.parse(trade_msg()) == TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3)))


def test_lenient_stream_skips_messages_without_a_checksum() -> None:
    stream = KrakenStream("BTC/USD", 10, require_checksum=False)
    no_checksum = book_msg("update").replace(', "checksum": 123', "")
    assert "checksum" not in no_checksum
    assert isinstance(stream.parse(no_checksum), BookUpdate)
    raw = DOC_SNAPSHOT.replace(',"checksum":3310070434', "")
    assert isinstance(stream.parse(raw), BookUpdate)
    corrupted = doc_update('{"price":45283.4,"qty":0.00000000}', "", DOC_CHECKSUM)
    with pytest.raises(BookChecksumError):
        stream.parse(corrupted)


def test_lenient_stream_still_needs_a_snapshot_before_a_checksum() -> None:
    stream = KrakenStream("BTC/USD", 10, require_checksum=False)
    with pytest.raises(KrakenMessageError, match="before the snapshot"):
        stream.parse(doc_update("", "", 0))


def test_book_subscription_reads_a_book_ack() -> None:
    ack = {
        "method": "subscribe",
        "result": {"channel": "book", "depth": 25, "snapshot": True, "symbol": "BTC/USD"},
        "success": True,
    }
    assert book_subscription(ack) == ("BTC/USD", 25)


@pytest.mark.parametrize(
    "msg",
    [
        {
            "method": "subscribe",
            "result": {"channel": "trade", "symbol": "BTC/USD"},
            "success": True,
        },
        {"method": "subscribe", "result": {}, "success": True},
        {"channel": "heartbeat"},
        {"method": "subscribe", "success": False, "error": "nope"},
    ],
    ids=["trade-ack", "empty-result", "heartbeat", "failed"],
)
def test_book_subscription_ignores_other_messages(msg: dict[str, Any]) -> None:
    assert book_subscription(msg) is None


@pytest.mark.parametrize(
    "result",
    [
        {"channel": "book", "depth": 7, "symbol": "BTC/USD"},
        {"channel": "book", "depth": True, "symbol": "BTC/USD"},
        {"channel": "book", "depth": "10", "symbol": "BTC/USD"},
        {"channel": "book", "symbol": "BTC/USD"},
        {"channel": "book", "depth": 10, "symbol": ""},
        {"channel": "book", "depth": 10, "symbol": 5},
    ],
    ids=["bad-depth", "bool-depth", "string-depth", "no-depth", "empty-symbol", "int-symbol"],
)
def test_book_subscription_rejects_a_malformed_book_ack(result: dict[str, Any]) -> None:
    with pytest.raises(KrakenMessageError, match="book subscription"):
        book_subscription({"method": "subscribe", "result": result, "success": True})


def test_stream_verifies_a_live_recording_message_by_message() -> None:
    fixture = Path(__file__).parent / "fixtures" / "kraken_btcusd_live_checksums.jsonl"
    stream = KrakenStream("BTC/USD", 10)
    books = 0
    for line in fixture.read_text(encoding="utf-8").splitlines():
        raw = line[line.index('"msg": ') + len('"msg": ') : -1]
        if isinstance(stream.parse(raw), BookUpdate):
            books += 1
    assert books == 24
