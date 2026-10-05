from __future__ import annotations

from collections.abc import Callable, Mapping

from slipstream.coinbase import COINBASE_WS_URL, CoinbaseStream, subscribe_messages
from slipstream.coinbase import MAX_MESSAGE_BYTES as COINBASE_MAX_BYTES
from slipstream.kraken import (
    KRAKEN_WS_URL,
    KrakenStream,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken import MAX_MESSAGE_BYTES as KRAKEN_MAX_BYTES
from slipstream.models import BookUpdate, TradeBatch, Venue

IDLE_TIMEOUT_S = 30.0

Parser = Callable[[str | bytes], BookUpdate | TradeBatch | None]


def endpoints(urls: Mapping[Venue, str] | None = None) -> dict[Venue, str]:
    resolved: dict[Venue, str] = {"kraken": KRAKEN_WS_URL, "coinbase": COINBASE_WS_URL}
    resolved.update(urls or {})
    return resolved


def max_message_bytes(venue: Venue) -> int:
    return COINBASE_MAX_BYTES if venue == "coinbase" else KRAKEN_MAX_BYTES


def subscriptions(venue: Venue, symbol: str, depth: int) -> list[str]:
    if venue == "coinbase":
        return subscribe_messages(symbol)
    return [subscribe_message(symbol, depth), subscribe_trades_message(symbol)]


def parser(venue: Venue, symbol: str, depth: int) -> Parser:
    """A fresh parser for one connection: sequence numbers and book checksums are per connection."""
    if venue == "coinbase":
        return CoinbaseStream(symbol, depth).parse
    return KrakenStream(symbol, depth).parse
