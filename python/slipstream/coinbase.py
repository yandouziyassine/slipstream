from __future__ import annotations

import json
import math
from typing import Any

from slipstream.models import BookUpdate, TradeBatch

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
MAX_MESSAGE_BYTES = 16 << 20
MAX_EVENTS = 16
MAX_UPDATES = 200_000
MAX_TRADES = 1000
MAX_DEPTH = 1000
MAX_FIELD_CHARS = 64
_PRODUCTS = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}
_SIDES = ("bid", "offer")


class CoinbaseMessageError(ValueError):
    pass


def product_id(symbol: str) -> str:
    try:
        return _PRODUCTS[symbol]
    except KeyError as exc:
        raise CoinbaseMessageError(f"no Coinbase product for {symbol!r}") from exc


def subscribe_messages(symbol: str) -> list[str]:
    product = product_id(symbol)
    return [
        json.dumps({"type": "subscribe", "channel": channel, "product_ids": [product]})
        for channel in ("level2", "market_trades", "heartbeats")
    ]


class CoinbaseStream:
    """Parses one Coinbase WebSocket connection; sequence numbers are per connection."""

    def __init__(self, symbol: str, depth: int) -> None:
        if not 1 <= depth <= MAX_DEPTH:
            raise CoinbaseMessageError("depth must be between 1 and 1000")
        self._symbol = symbol
        self._product = product_id(symbol)
        self._depth = depth
        self._last_sequence: int | None = None

    def parse(self, raw: str | bytes) -> BookUpdate | TradeBatch | None:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise CoinbaseMessageError("message too large")
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise CoinbaseMessageError("invalid JSON") from exc
        if not isinstance(msg, dict):
            raise CoinbaseMessageError("message is not an object")
        if msg.get("type") == "error":
            message = msg.get("message")
            detail = message[:200] if isinstance(message, str) else type(message).__name__
            raise CoinbaseMessageError(f"coinbase error: {detail!r}")
        self._check_sequence(msg.get("sequence_num"))
        channel = msg.get("channel")
        if channel == "l2_data":
            return self._book(_events(msg))
        if channel == "market_trades":
            return self._trades(_events(msg))
        return None

    def _check_sequence(self, sequence: Any) -> None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CoinbaseMessageError("invalid sequence_num")
        if self._last_sequence is not None and sequence != self._last_sequence + 1:
            raise CoinbaseMessageError("sequence gap")
        self._last_sequence = sequence

    def _book(self, events: list[dict[str, Any]]) -> BookUpdate:
        kinds: set[str] = set()
        for event in events:
            kind = event.get("type")
            if not isinstance(kind, str):
                raise CoinbaseMessageError("level2 event type must be a string")
            kinds.add(kind)
        if kinds - {"snapshot", "update"} or len(kinds) != 1:
            raise CoinbaseMessageError(f"unexpected level2 event types: {sorted(kinds)}")
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for event in events:
            if event.get("product_id") != self._product:
                raise CoinbaseMessageError("unexpected product")
            updates = event.get("updates")
            if not isinstance(updates, list) or len(updates) > MAX_UPDATES:
                raise CoinbaseMessageError("updates must be a bounded list")
            for update in updates:
                if not isinstance(update, dict) or update.get("side") not in _SIDES:
                    raise CoinbaseMessageError("invalid level2 update")
                price = _decimal(update.get("price_level"), "price", allow_zero=False)
                qty = _decimal(update.get("new_quantity"), "quantity", allow_zero=True)
                (bids if update["side"] == "bid" else asks).append((price, qty))
        is_snapshot = kinds == {"snapshot"}
        if is_snapshot:
            bids = sorted((level for level in bids if level[1] > 0), reverse=True)[: self._depth]
            asks = sorted(level for level in asks if level[1] > 0)[: self._depth]
        return BookUpdate(self._symbol, is_snapshot, tuple(bids), tuple(asks), "coinbase")

    def _trades(self, events: list[dict[str, Any]]) -> TradeBatch | None:
        prints: list[tuple[float, float]] = []
        for event in events:
            kind = event.get("type")
            if kind not in ("snapshot", "update"):
                raise CoinbaseMessageError(f"unexpected trade event type: {kind!r}")
            rows = event.get("trades")
            if not isinstance(rows, list) or len(rows) > MAX_TRADES:
                raise CoinbaseMessageError("trades must be a bounded list")
            if kind == "snapshot":
                continue
            for row in rows:
                if not isinstance(row, dict) or row.get("product_id") != self._product:
                    raise CoinbaseMessageError("invalid trade")
                price = _decimal(row.get("price"), "trade price", allow_zero=False)
                size = _decimal(row.get("size"), "trade size", allow_zero=False)
                prints.append((price, size))
        if not prints:
            return None
        return TradeBatch(self._symbol, False, tuple(prints), "coinbase")


def _events(msg: dict[str, Any]) -> list[dict[str, Any]]:
    events = msg.get("events")
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise CoinbaseMessageError("events must be a non-empty bounded list")
    if not all(isinstance(event, dict) for event in events):
        raise CoinbaseMessageError("event must be an object")
    return events


def _decimal(value: Any, name: str, *, allow_zero: bool) -> float:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise CoinbaseMessageError(f"{name} must be a short decimal string")
    try:
        result = float(value)
    except ValueError as exc:
        raise CoinbaseMessageError(f"{name} is not a number") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise CoinbaseMessageError(f"{name} out of range")
    return result
