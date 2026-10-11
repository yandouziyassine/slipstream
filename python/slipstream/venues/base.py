"""The interface every venue implements; see the 2026-10-10 venue registry design spec."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from slipstream.models import BookUpdate, MarketDataError, TradeBatch
from slipstream.venue_rules import VenueRules


@dataclass(frozen=True)
class Reply:
    """A message the venue requires us to send back on the same connection."""

    text: str


# None means a heartbeat or a message with nothing for the engine.
Parsed = BookUpdate | TradeBatch | Reply | None
Parser = Callable[[str | bytes], Parsed]
Fetch = Callable[[str], bytes]


class RecordedCheck(Protocol):
    """An extra check of one venue's recorded messages at replay."""

    def check(self, msg: Any) -> None: ...

    def reset(self) -> None: ...


@dataclass(frozen=True)
class VenueAdapter:
    name: str
    ws_url: str
    max_message_bytes: int
    # The public fee page's lowest-tier taker fee, for reference: runs take the user's own tier.
    entry_taker_fee_bps: float
    subscriptions: Callable[[str, int], list[str]]
    # A fresh parser per connection: sequence numbers and book mirrors are per connection.
    new_parser: Callable[[str, int], Parser]
    fetch_rules: Callable[[str, Fetch], VenueRules]
    new_replay_parser: Callable[[str, int], Parser] | None = None
    new_recorded_check: Callable[[], RecordedCheck] | None = None
    # Errors a fresh connection recovers from; any other market data error stops the feed.
    resync_errors: tuple[type[MarketDataError], ...] = ()

    def replay_parser(self, symbol: str, depth: int) -> Parser:
        new = self.new_replay_parser or self.new_parser
        return new(symbol, depth)
