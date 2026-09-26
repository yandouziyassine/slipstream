from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from slipstream.book import LocalBook
from slipstream.coinbase import CoinbaseMessageError, CoinbaseStream, product_id
from slipstream.kraken import KrakenMessageError, parse_message
from slipstream.models import BookUpdate, TradeBatch
from slipstream.replay import ReplayError, read_replay
from slipstream.venue_rules import (
    VenueRules,
    VenueRulesError,
    parse_coinbase_rules,
    parse_kraken_rules,
)

pt_settings = settings(max_examples=200, deadline=None, derandomize=True)


def _json_scalars() -> st.SearchStrategy[Any]:
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**18), max_value=10**18),
        st.floats(allow_nan=True, allow_infinity=True, width=64),
        st.text(max_size=20),
    )


def _json_values() -> st.SearchStrategy[Any]:
    return st.recursive(
        _json_scalars(),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(max_size=10), children, max_size=4),
        ),
        max_leaves=12,
    )


def _raw_message_strategy() -> st.SearchStrategy[str | bytes]:
    """Hostile input covering plain garbage, binary garbage, and arbitrary JSON-ish text."""
    return st.one_of(
        st.text(max_size=1000),
        st.binary(max_size=1000),
        _json_values().map(json.dumps),
        st.text(alphabet='[]{}":,0123456789', max_size=300),
    )


def _raw_bytes_strategy() -> st.SearchStrategy[bytes]:
    return st.one_of(
        st.binary(max_size=1000),
        _json_values().map(lambda v: json.dumps(v).encode("utf-8")),
    )


def _assert_sorted_unique_positive(
    levels: tuple[tuple[float, float], ...], *, descending: bool, depth: int
) -> None:
    assert len(levels) <= depth
    prices = [price for price, _ in levels]
    assert prices == sorted(prices, reverse=descending)
    assert len(set(prices)) == len(prices)
    for _, qty in levels:
        assert qty > 0


# --- Kraken parser ---


@given(raw=_raw_message_strategy())
@pt_settings
def test_kraken_parse_message_only_raises_kraken_error(raw: str | bytes) -> None:
    try:
        result = parse_message(raw)
    except KrakenMessageError:
        return
    assert result is None or isinstance(result, (BookUpdate, TradeBatch))


_KRAKEN_PRICE = st.floats(min_value=1e-9, max_value=1e12, allow_nan=False, allow_infinity=False)
_KRAKEN_QTY = st.floats(min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False)
_KRAKEN_LEVELS = st.lists(st.tuples(_KRAKEN_PRICE, _KRAKEN_QTY), max_size=15)


@given(
    msg_type=st.sampled_from(["snapshot", "update"]),
    symbol=st.text(min_size=1, max_size=12),
    bids=_KRAKEN_LEVELS,
    asks=_KRAKEN_LEVELS,
)
@pt_settings
def test_kraken_valid_book_levels_round_trip(
    msg_type: str, symbol: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]
) -> None:
    raw = json.dumps(
        {
            "channel": "book",
            "type": msg_type,
            "data": [
                {
                    "symbol": symbol,
                    "bids": [{"price": p, "qty": q} for p, q in bids],
                    "asks": [{"price": p, "qty": q} for p, q in asks],
                }
            ],
        }
    )
    update = parse_message(raw)
    assert isinstance(update, BookUpdate)
    assert update.symbol == symbol
    assert update.is_snapshot == (msg_type == "snapshot")
    assert update.bids == tuple(bids)
    assert update.asks == tuple(asks)
    for price, qty in update.bids + update.asks:
        assert math.isfinite(price) and price > 0
        assert math.isfinite(qty) and qty >= 0


# --- Coinbase stream ---


@given(raw=_raw_message_strategy())
@pt_settings
def test_coinbase_parse_only_raises_coinbase_error(raw: str | bytes) -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    try:
        result = stream.parse(raw)
    except CoinbaseMessageError:
        return
    assert result is None or isinstance(result, (BookUpdate, TradeBatch))


_CB_PRICE = st.integers(min_value=1, max_value=500)
_CB_QTY_POS = st.integers(min_value=1, max_value=200)
_CB_QTY_NONNEG = st.integers(min_value=0, max_value=200)
_CB_LEVELS = st.lists(st.tuples(_CB_PRICE, _CB_QTY_POS), max_size=8, unique_by=lambda t: t[0])
_CB_UPDATES = st.lists(
    st.tuples(st.sampled_from(["bid", "offer"]), _CB_PRICE, _CB_QTY_NONNEG), max_size=15
)


def _cb_level_update(side: str, price: int, qty: int) -> dict[str, str]:
    return {
        "side": side,
        "event_time": "2026-09-24T00:00:00Z",
        "price_level": str(price),
        "new_quantity": str(qty),
    }


def _cb_l2_message(seq: int, kind: str, updates: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "client_id": "",
            "timestamp": "2026-09-24T00:00:00Z",
            "sequence_num": seq,
            "events": [{"type": kind, "product_id": product_id("BTC/USD"), "updates": updates}],
        }
    )


@given(
    depth=st.integers(min_value=1, max_value=6),
    snap_bids=_CB_LEVELS,
    snap_asks=_CB_LEVELS,
    updates=_CB_UPDATES,
)
@pt_settings
def test_coinbase_stream_matches_reference_model(
    depth: int,
    snap_bids: list[tuple[int, int]],
    snap_asks: list[tuple[int, int]],
    updates: list[tuple[str, int, int]],
) -> None:
    stream = CoinbaseStream("BTC/USD", depth=depth)
    ref_bids: dict[float, float] = {float(p): float(q) for p, q in snap_bids}
    ref_asks: dict[float, float] = {float(p): float(q) for p, q in snap_asks}

    def assert_matches(update: BookUpdate) -> None:
        exp_bids = tuple(sorted(ref_bids.items(), key=lambda kv: -kv[0])[:depth])
        exp_asks = tuple(sorted(ref_asks.items())[:depth])
        assert update.bids == exp_bids
        assert update.asks == exp_asks
        assert update.venue == "coinbase"
        _assert_sorted_unique_positive(update.bids, descending=True, depth=depth)
        _assert_sorted_unique_positive(update.asks, descending=False, depth=depth)

    snapshot_updates = [_cb_level_update("bid", p, q) for p, q in snap_bids] + [
        _cb_level_update("offer", p, q) for p, q in snap_asks
    ]
    result = stream.parse(_cb_l2_message(0, "snapshot", snapshot_updates))
    assert isinstance(result, BookUpdate)
    assert_matches(result)

    for seq, (side, price, qty) in enumerate(updates, start=1):
        target = ref_bids if side == "bid" else ref_asks
        if qty == 0:
            target.pop(float(price), None)
        else:
            target[float(price)] = float(qty)
        result = stream.parse(_cb_l2_message(seq, "update", [_cb_level_update(side, price, qty)]))
        assert isinstance(result, BookUpdate)
        assert_matches(result)


# --- Venue rules ---


_NONNEG_DECIMAL = st.integers(min_value=0, max_value=10**9).map(str)


@given(raw=_raw_bytes_strategy())
@pt_settings
def test_kraken_rules_hostile_input_only_raises(raw: bytes) -> None:
    try:
        result = parse_kraken_rules(raw)
    except VenueRulesError:
        return
    assert isinstance(result, VenueRules)


@given(raw=_raw_bytes_strategy())
@pt_settings
def test_coinbase_rules_hostile_input_only_raises(raw: bytes) -> None:
    try:
        result = parse_coinbase_rules(raw)
    except VenueRulesError:
        return
    assert isinstance(result, VenueRules)


@given(
    pair_key=st.text(min_size=1, max_size=10),
    ordermin=_NONNEG_DECIMAL,
    costmin=_NONNEG_DECIMAL,
    lot_decimals=st.integers(min_value=0, max_value=12),
)
@pt_settings
def test_kraken_rules_valid_payload_round_trips(
    pair_key: str, ordermin: str, costmin: str, lot_decimals: int
) -> None:
    body = json.dumps(
        {
            "error": [],
            "result": {
                pair_key: {
                    "ordermin": ordermin,
                    "costmin": costmin,
                    "lot_decimals": lot_decimals,
                }
            },
        }
    ).encode()
    rules = parse_kraken_rules(body, pair_key=pair_key)
    assert rules == VenueRules(
        min_qty=float(ordermin), qty_step=10.0**-lot_decimals, min_notional=float(costmin)
    )


@given(
    product=st.text(min_size=1, max_size=10),
    base_min_size=_NONNEG_DECIMAL,
    base_increment=_NONNEG_DECIMAL,
    quote_min_size=_NONNEG_DECIMAL,
)
@pt_settings
def test_coinbase_rules_valid_payload_round_trips(
    product: str, base_min_size: str, base_increment: str, quote_min_size: str
) -> None:
    body = json.dumps(
        {
            "product_id": product,
            "status": "online",
            "trading_disabled": False,
            "base_min_size": base_min_size,
            "base_increment": base_increment,
            "quote_min_size": quote_min_size,
        }
    ).encode()
    rules = parse_coinbase_rules(body, product=product)
    assert rules == VenueRules(
        min_qty=float(base_min_size),
        qty_step=float(base_increment),
        min_notional=float(quote_min_size),
    )


# --- Replay reader ---


@given(content=st.one_of(st.text(max_size=2000), st.binary(max_size=2000)))
@pt_settings
def test_replay_hostile_input_only_raises(content: str | bytes) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "replay.jsonl"
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        try:
            list(read_replay(path))
        except ReplayError:
            return


# --- LocalBook ---


_LB_PRICE = st.integers(min_value=1, max_value=500)
_LB_QTY_POS = st.integers(min_value=1, max_value=200)
_LB_QTY_NONNEG = st.integers(min_value=0, max_value=200)
_LB_LEVELS = st.lists(st.tuples(_LB_PRICE, _LB_QTY_POS), max_size=8, unique_by=lambda t: t[0])
_LB_DELTAS = st.lists(
    st.tuples(st.sampled_from(["bid", "ask"]), _LB_PRICE, _LB_QTY_NONNEG), max_size=15
)


@given(
    depth=st.integers(min_value=1, max_value=6),
    snap_bids=_LB_LEVELS,
    snap_asks=_LB_LEVELS,
    deltas=_LB_DELTAS,
)
@pt_settings
def test_local_book_matches_reference_model(
    depth: int,
    snap_bids: list[tuple[int, int]],
    snap_asks: list[tuple[int, int]],
    deltas: list[tuple[str, int, int]],
) -> None:
    book = LocalBook(depth=depth)
    ref_bids: dict[float, float] = {float(p): float(q) for p, q in snap_bids}
    ref_asks: dict[float, float] = {float(p): float(q) for p, q in snap_asks}

    def assert_matches() -> None:
        exp_bids = tuple(sorted(ref_bids.items(), key=lambda kv: -kv[0])[:depth])
        exp_asks = tuple(sorted(ref_asks.items())[:depth])
        assert book.bids() == exp_bids
        assert book.asks() == exp_asks
        _assert_sorted_unique_positive(book.bids(), descending=True, depth=depth)
        _assert_sorted_unique_positive(book.asks(), descending=False, depth=depth)

    book.apply(
        BookUpdate(
            "BTC/USD",
            True,
            tuple((float(p), float(q)) for p, q in snap_bids),
            tuple((float(p), float(q)) for p, q in snap_asks),
        )
    )
    assert_matches()

    for side, price, qty in deltas:
        target = ref_bids if side == "bid" else ref_asks
        if qty == 0:
            target.pop(float(price), None)
        else:
            target[float(price)] = float(qty)
        bids_delta = ((float(price), float(qty)),) if side == "bid" else ()
        asks_delta = ((float(price), float(qty)),) if side == "ask" else ()
        book.apply(BookUpdate("BTC/USD", False, bids_delta, asks_delta))
        assert_matches()
