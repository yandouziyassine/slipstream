from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from websockets.exceptions import WebSocketException

from slipstream.coinbase import COINBASE_WS_URL, CoinbaseStream, subscribe_messages
from slipstream.coinbase import MAX_MESSAGE_BYTES as COINBASE_MAX_BYTES
from slipstream.kraken import (
    KRAKEN_WS_URL,
    KrakenStream,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken import MAX_MESSAGE_BYTES as KRAKEN_MAX_BYTES
from slipstream.kraken_checksum import BookChecksumError
from slipstream.models import BookUpdate, TradeBatch, Venue

IDLE_TIMEOUT_S = 30.0
MAX_REASON_CHARS = 200

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


class IdleTimeoutError(ConnectionError):
    """No message within the idle timeout: the connection is as good as lost."""


@dataclass(frozen=True)
class ReconnectPolicy:
    max_reconnects: int = 5
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0

    def __post_init__(self) -> None:
        if self.max_reconnects < 0:
            raise ValueError("max_reconnects must not be negative")
        if not (math.isfinite(self.base_delay_s) and math.isfinite(self.max_delay_s)):
            raise ValueError("reconnect delays must be finite")
        if not 0 < self.base_delay_s <= self.max_delay_s:
            raise ValueError("reconnect delays must satisfy 0 < base_delay_s <= max_delay_s")

    def delay_s(self, attempt: int, rng: Callable[[], float] = random.random) -> float:
        """Equal jitter: somewhere in the upper half of the doubled, capped delay."""
        ceiling = min(self.max_delay_s, self.base_delay_s * 2.0 ** (attempt - 1))
        return ceiling * (0.5 + rng() / 2)


DEFAULT_RECONNECT = ReconnectPolicy()


@dataclass(frozen=True)
class FeedReconnect:
    venue: Venue
    attempt: int
    reason: str
    downtime_s: float
    recovered: bool


def is_reconnectable(error: BaseException) -> bool:
    # A checksum mismatch is retried too: Kraken's documented recovery is a fresh snapshot.
    return isinstance(error, (OSError, WebSocketException, BookChecksumError))


def describe(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    return "".join(char if char.isprintable() else " " for char in text)[:MAX_REASON_CHARS]


def give_up_message(venue: Venue, max_reconnects: int, error: BaseException) -> str:
    return f"{venue} feed gave up after {max_reconnects} reconnects: {describe(error)}"


class ReconnectTracker:
    """Counts one venue's connection failures and reports each reconnect once its downtime is
    known: at the new connection's first book snapshot, at the next failure, or at close().
    """

    def __init__(
        self,
        venue: Venue,
        policy: ReconnectPolicy,
        report: Callable[[FeedReconnect], None],
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._venue = venue
        self._policy = policy
        self._report = report
        self._clock = clock
        self._rng = rng
        self._failures = 0
        self._pending: tuple[int, str, float] | None = None

    @property
    def policy(self) -> ReconnectPolicy:
        return self._policy

    @property
    def failures(self) -> int:
        return self._failures

    def failed(self, error: BaseException) -> float | None:
        """The backoff before the next connection, or None once no reconnect is left."""
        now = self._clock()
        self._settle(now, recovered=False)
        self._failures += 1
        if self._failures > self._policy.max_reconnects:
            return None
        self._pending = (self._failures, describe(error), now)
        return self._policy.delay_s(self._failures, self._rng)

    def received(self, update: BookUpdate | TradeBatch | None) -> None:
        if self._pending is not None and isinstance(update, BookUpdate) and update.is_snapshot:
            self._settle(self._clock(), recovered=True)

    def close(self) -> None:
        self._settle(self._clock(), recovered=False)

    def _settle(self, now: float, recovered: bool) -> None:
        if self._pending is None:
            return
        attempt, reason, since = self._pending
        self._pending = None
        self._report(FeedReconnect(self._venue, attempt, reason, now - since, recovered))
