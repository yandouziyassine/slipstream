from __future__ import annotations

from slipstream.coinbase import (
    COINBASE_WS_URL,
    MAX_MESSAGE_BYTES,
    CoinbaseStream,
    subscribe_messages,
)
from slipstream.venue_rules import coinbase_rules
from slipstream.venues.base import Parser, VenueAdapter


def _subscriptions(symbol: str, depth: int) -> list[str]:
    return subscribe_messages(symbol)


def _parser(symbol: str, depth: int) -> Parser:
    return CoinbaseStream(symbol, depth).parse


COINBASE = VenueAdapter(
    name="coinbase",
    ws_url=COINBASE_WS_URL,
    max_message_bytes=MAX_MESSAGE_BYTES,
    # Not published without sign-in; the project's illustrative entry-tier figure.
    entry_taker_fee_bps=60.0,
    subscriptions=_subscriptions,
    new_parser=_parser,
    fetch_rules=coinbase_rules,
)
