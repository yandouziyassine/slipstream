from __future__ import annotations

from typing import Any

from slipstream.kraken import (
    KRAKEN_WS_URL,
    MAX_MESSAGE_BYTES,
    KrakenStream,
    book_subscription,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken_checksum import BookChecksumError
from slipstream.venue_rules import kraken_rules
from slipstream.venues.base import Parser, VenueAdapter


def _subscriptions(symbol: str, depth: int) -> list[str]:
    return [subscribe_message(symbol, depth), subscribe_trades_message(symbol)]


def _parser(symbol: str, depth: int) -> Parser:
    return KrakenStream(symbol, depth).parse


def _replay_parser(symbol: str, depth: int) -> Parser:
    # Replay re-checks recorded checksums once, in RecordedBookCheck; hand-written files have none.
    return parse_message


class RecordedBookCheck:
    """Checks recorded Kraken book checksums from the recorded book subscription on.

    The recorder always writes Kraken's subscribe ack first; before one there is no depth to
    mirror the book at, so hand-written files without it are not checked.
    """

    def __init__(self) -> None:
        self._stream: KrakenStream | None = None

    def check(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        subscription = book_subscription(msg)
        if subscription is not None:
            self._stream = KrakenStream(*subscription, require_checksum=False)
        elif self._stream is not None:
            self._stream.accept(msg)

    def reset(self) -> None:
        self._stream = None


KRAKEN = VenueAdapter(
    name="kraken",
    ws_url=KRAKEN_WS_URL,
    max_message_bytes=MAX_MESSAGE_BYTES,
    entry_taker_fee_bps=80.0,
    subscriptions=_subscriptions,
    new_parser=_parser,
    fetch_rules=kraken_rules,
    new_replay_parser=_replay_parser,
    new_recorded_check=RecordedBookCheck,
    # Kraken's documented recovery from a checksum mismatch is a fresh snapshot.
    resync_errors=(BookChecksumError,),
)
