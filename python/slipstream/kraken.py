from __future__ import annotations

import json
import math
from typing import Any

from slipstream.models import BookUpdate, TradeBatch

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
MAX_LEVELS = 1000
MAX_MESSAGE_BYTES = 1 << 20


class KrakenMessageError(ValueError):
    pass


def subscribe_message(symbol: str, depth: int) -> str:
    return json.dumps(
        {
            "method": "subscribe",
            "params": {"channel": "book", "symbol": [symbol], "depth": depth, "snapshot": True},
        }
    )


def subscribe_trades_message(symbol: str) -> str:
    return json.dumps(
        {
            "method": "subscribe",
            "params": {"channel": "trade", "symbol": [symbol], "snapshot": False},
        }
    )


def parse_message(raw: str | bytes) -> BookUpdate | TradeBatch | None:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise KrakenMessageError("message too large")
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise KrakenMessageError("invalid JSON") from exc
    if not isinstance(msg, dict):
        raise KrakenMessageError("message is not an object")

    if msg.get("method") == "subscribe":
        if msg.get("success") is not True:
            raise KrakenMessageError(f"subscription failed: {msg.get('error')!r}")
        return None

    channel = msg.get("channel")
    if channel == "book":
        return _parse_book(msg)
    if channel == "trade":
        return _parse_trades(msg)
    return None


def _parse_book(msg: dict[str, Any]) -> BookUpdate:
    msg_type = msg.get("type")
    if msg_type not in ("snapshot", "update"):
        raise KrakenMessageError(f"unexpected book message type: {msg_type!r}")
    data = msg.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise KrakenMessageError("book data must be a single-element list")
    entry = data[0]
    symbol = entry.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise KrakenMessageError("missing symbol")
    return BookUpdate(
        symbol=symbol,
        is_snapshot=msg_type == "snapshot",
        bids=_parse_levels(entry.get("bids"), "bids"),
        asks=_parse_levels(entry.get("asks"), "asks"),
    )


def _parse_trades(msg: dict[str, Any]) -> TradeBatch:
    msg_type = msg.get("type")
    if msg_type not in ("snapshot", "update"):
        raise KrakenMessageError(f"unexpected trade message type: {msg_type!r}")
    data = msg.get("data")
    if not isinstance(data, list) or not data:
        raise KrakenMessageError("trade data must be a non-empty list")
    if len(data) > MAX_LEVELS:
        raise KrakenMessageError("too many trades")
    symbols: set[str] = set()
    trades: list[tuple[float, float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise KrakenMessageError("trade must be an object")
        symbol = entry.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise KrakenMessageError("trade missing symbol")
        symbols.add(symbol)
        price = _number(entry.get("price"), "trade price")
        qty = _number(entry.get("qty"), "trade qty")
        if price <= 0 or qty <= 0:
            raise KrakenMessageError("trade out of range")
        trades.append((price, qty))
    if len(symbols) != 1:
        raise KrakenMessageError("trade batch mixes symbols")
    return TradeBatch(symbols.pop(), msg_type == "snapshot", tuple(trades))


def _parse_levels(value: Any, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list):
        raise KrakenMessageError(f"{name} must be a list")
    if len(value) > MAX_LEVELS:
        raise KrakenMessageError(f"too many {name} levels")
    return tuple(_parse_level(level, name) for level in value)


def _parse_level(level: Any, name: str) -> tuple[float, float]:
    if not isinstance(level, dict):
        raise KrakenMessageError(f"{name} level must be an object")
    price = _number(level.get("price"), f"{name} price")
    qty = _number(level.get("qty"), f"{name} qty")
    if price <= 0 or qty < 0:
        raise KrakenMessageError(f"{name} level out of range")
    return price, qty


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KrakenMessageError(f"{name} must be a number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise KrakenMessageError(f"{name} out of range") from exc
    if not math.isfinite(result):
        raise KrakenMessageError(f"{name} must be finite")
    return result
