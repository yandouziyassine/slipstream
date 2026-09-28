from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from slipstream.kraken_checksum import BOOK_DEPTHS, BookChecksumError, KrakenBook, Level
from slipstream.models import BookUpdate, MarketDataError, TradeBatch

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
MAX_LEVELS = 1000
MAX_MESSAGE_BYTES = 1 << 20
MAX_CHECKSUM = (1 << 32) - 1
MAX_SYMBOL_CHARS = 32


class KrakenMessageError(MarketDataError):
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


@dataclass(frozen=True, slots=True)
class _Book:
    symbol: str
    is_snapshot: bool
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    checksum: int | None

    def update(self) -> BookUpdate:
        return BookUpdate(self.symbol, self.is_snapshot, _floats(self.bids), _floats(self.asks))


def parse_message(raw: str | bytes) -> BookUpdate | TradeBatch | None:
    """One message on its own; KrakenStream also checks book checksums across messages."""
    result = _dispatch(_load(raw))
    return result.update() if isinstance(result, _Book) else result


class KrakenStream:
    """Parses one Kraken book subscription, checking every book message against its checksum."""

    def __init__(self, symbol: str, depth: int, *, require_checksum: bool = True) -> None:
        self._symbol = symbol
        self._book = KrakenBook(depth)
        self._require_checksum = require_checksum
        self._synced = False

    def parse(self, raw: str | bytes) -> BookUpdate | TradeBatch | None:
        return self.accept(_load(raw))

    def accept(self, msg: dict[str, Any]) -> BookUpdate | TradeBatch | None:
        """Like parse, for a message already decoded with Decimal floats."""
        result = _dispatch(msg)
        if isinstance(result, _Book):
            self._check(result)
            return result.update()
        return result

    def _check(self, book: _Book) -> None:
        if book.symbol != self._symbol:
            raise KrakenMessageError(f"unexpected symbol {book.symbol[:MAX_SYMBOL_CHARS]!r}")
        if book.checksum is None and self._require_checksum:
            raise KrakenMessageError("book message missing checksum")
        if book.is_snapshot:
            self._synced = True
        elif not self._synced:
            if book.checksum is None:
                return
            raise KrakenMessageError("book update before the snapshot")
        self._book.apply(book.is_snapshot, book.bids, book.asks)
        if book.checksum is None:
            return
        computed = self._book.checksum()
        if computed != book.checksum:
            raise BookChecksumError(
                f"kraken {self._symbol} book checksum mismatch: "
                f"expected {book.checksum}, computed {computed}"
            )


def book_subscription(msg: dict[str, Any]) -> tuple[str, int] | None:
    """The (symbol, depth) a successful book subscribe ack confirms; None for other messages."""
    if msg.get("method") != "subscribe" or msg.get("success") is not True:
        return None
    result = msg.get("result")
    if not isinstance(result, dict) or result.get("channel") != "book":
        return None
    symbol = result.get("symbol")
    depth = result.get("depth")
    if not isinstance(symbol, str) or not 0 < len(symbol) <= MAX_SYMBOL_CHARS:
        raise KrakenMessageError("malformed book subscription symbol")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth not in BOOK_DEPTHS:
        raise KrakenMessageError("malformed book subscription depth")
    return symbol, depth


def _load(raw: str | bytes) -> dict[str, Any]:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise KrakenMessageError("message too large")
    try:
        # Decimal keeps each number's wire text, which the book checksum is computed over.
        msg = json.loads(raw, parse_float=Decimal)
    except (ValueError, RecursionError) as exc:
        raise KrakenMessageError("invalid JSON") from exc
    if not isinstance(msg, dict):
        raise KrakenMessageError("message is not an object")
    return msg


def _dispatch(msg: dict[str, Any]) -> _Book | TradeBatch | None:
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


def _parse_book(msg: dict[str, Any]) -> _Book:
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
    return _Book(
        symbol=symbol,
        is_snapshot=msg_type == "snapshot",
        bids=_parse_levels(entry.get("bids"), "bids"),
        asks=_parse_levels(entry.get("asks"), "asks"),
        checksum=_parse_checksum(entry),
    )


def _parse_checksum(entry: dict[str, Any]) -> int | None:
    if "checksum" not in entry:
        return None
    value = entry["checksum"]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CHECKSUM:
        raise KrakenMessageError("checksum must be an unsigned 32-bit integer")
    return value


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
        price = float(_number(entry.get("price"), "trade price"))
        qty = float(_number(entry.get("qty"), "trade qty"))
        if price <= 0 or qty <= 0:
            raise KrakenMessageError("trade out of range")
        trades.append((price, qty))
    if len(symbols) != 1:
        raise KrakenMessageError("trade batch mixes symbols")
    return TradeBatch(symbols.pop(), msg_type == "snapshot", tuple(trades))


def _parse_levels(value: Any, name: str) -> tuple[Level, ...]:
    if not isinstance(value, list):
        raise KrakenMessageError(f"{name} must be a list")
    if len(value) > MAX_LEVELS:
        raise KrakenMessageError(f"too many {name} levels")
    return tuple(_parse_level(level, name) for level in value)


def _parse_level(level: Any, name: str) -> Level:
    if not isinstance(level, dict):
        raise KrakenMessageError(f"{name} level must be an object")
    price = _number(level.get("price"), f"{name} price")
    qty = _number(level.get("qty"), f"{name} qty")
    # Compare as float too: a positive decimal can still round to a 0.0 price.
    if float(price) <= 0 or qty < 0:
        raise KrakenMessageError(f"{name} level out of range")
    return price, qty


def _number(value: Any, name: str) -> Decimal:
    # NaN and Infinity decode as float: JSON numbers are always int or Decimal here.
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise KrakenMessageError(f"{name} must be a finite number")
    result = Decimal(value)
    if not math.isfinite(float(result)):
        raise KrakenMessageError(f"{name} out of range")
    return result


def _floats(levels: tuple[Level, ...]) -> tuple[tuple[float, float], ...]:
    return tuple((float(price), float(qty)) for price, qty in levels)
