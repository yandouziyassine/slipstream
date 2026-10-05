from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from slipstream.kraken import KrakenMessageError, KrakenStream, book_subscription
from slipstream.kraken_rest import SUPPORTED_INTERVALS, Bar, parse_ohlc
from slipstream.models import VENUES, MarketDataError, Venue

_VALID_VENUES: frozenset[Venue] = frozenset(VENUES)


class ReplayError(ValueError):
    pass


@dataclass(frozen=True)
class ReconnectMarker:
    """The recorder lost this venue's connection here: its book is unknown until a new snapshot."""

    recv_ns: int
    venue: Venue


ReplayRecord = tuple[int, str, Venue] | ReconnectMarker


class _FloatEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _dumps(value: Any) -> str:
    return json.dumps(value, cls=_FloatEncoder)


class _KrakenBookCheck:
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


def _records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        lineno = 0
        while True:
            lineno += 1
            try:
                line = handle.readline()
            except UnicodeDecodeError as exc:
                raise ReplayError(f"line {lineno}: invalid UTF-8") from exc
            if line == "":
                return
            text = line.strip()
            if not text:
                continue
            try:
                # Decimal keeps recorded numbers' text, which Kraken book checksums cover.
                record = json.loads(text, parse_float=Decimal)
            except (ValueError, RecursionError) as exc:
                raise ReplayError(f"line {lineno}: malformed replay record") from exc
            if not isinstance(record, dict):
                raise ReplayError(f"line {lineno}: malformed replay record")
            yield lineno, record


def _is_ohlc(record: dict[str, Any]) -> bool:
    return record.get("kind") == "ohlc"


def _recv_ns(record: dict[str, Any], lineno: int) -> int:
    recv_ns = record.get("recv_ns")
    if isinstance(recv_ns, bool) or not isinstance(recv_ns, int) or recv_ns < 0:
        raise ReplayError(f"line {lineno}: recv_ns must be a non-negative integer")
    return recv_ns


def _venue(record: dict[str, Any], lineno: int) -> Venue:
    venue = record.get("venue", "kraken")
    if isinstance(venue, bool) or not isinstance(venue, str) or venue not in _VALID_VENUES:
        raise ReplayError(f"line {lineno}: unsupported venue {venue!r}")
    return venue


def read_replay(path: Path) -> Iterator[ReplayRecord]:
    kraken_books = _KrakenBookCheck()
    for lineno, record in _records(path):
        if _is_ohlc(record):
            continue
        if record.get("kind") == "reconnect":
            marker = ReconnectMarker(_recv_ns(record, lineno), _venue(record, lineno))
            if marker.venue == "kraken":
                kraken_books.reset()
            yield marker
            continue
        if "msg" not in record:
            raise ReplayError(f"line {lineno}: malformed replay record")
        recv_ns = _recv_ns(record, lineno)
        venue = _venue(record, lineno)
        if venue == "kraken":
            try:
                kraken_books.check(record["msg"])
            except MarketDataError as exc:
                raise ReplayError(f"line {lineno}: {exc}") from exc
        yield recv_ns, _dumps(record["msg"]), venue


def read_calibration(path: Path) -> dict[int, tuple[Bar, ...]]:
    bars: dict[int, tuple[Bar, ...]] = {}
    for lineno, record in _records(path):
        if not _is_ohlc(record):
            continue
        interval = record.get("interval")
        if isinstance(interval, bool) or interval not in SUPPORTED_INTERVALS:
            raise ReplayError(f"line {lineno}: unsupported OHLC interval")
        try:
            bars[interval] = parse_ohlc(_dumps(record.get("data")))
        except KrakenMessageError as exc:
            raise ReplayError(f"line {lineno}: invalid OHLC data: {exc}") from exc
    return bars
