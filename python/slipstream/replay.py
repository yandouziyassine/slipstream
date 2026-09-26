from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import SUPPORTED_INTERVALS, Bar, parse_ohlc
from slipstream.models import VENUES, Venue
from slipstream.runner import ExecutionRunner

_VALID_VENUES: frozenset[Venue] = frozenset(VENUES)


class ReplayError(ValueError):
    pass


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
                record = json.loads(text)
            except (ValueError, RecursionError) as exc:
                raise ReplayError(f"line {lineno}: malformed replay record") from exc
            if not isinstance(record, dict):
                raise ReplayError(f"line {lineno}: malformed replay record")
            yield lineno, record


def _is_ohlc(record: dict[str, Any]) -> bool:
    return record.get("kind") == "ohlc"


def read_replay(path: Path) -> Iterator[tuple[int, str, Venue]]:
    for lineno, record in _records(path):
        if _is_ohlc(record):
            continue
        recv_ns = record.get("recv_ns")
        if "msg" not in record:
            raise ReplayError(f"line {lineno}: malformed replay record")
        if isinstance(recv_ns, bool) or not isinstance(recv_ns, int) or recv_ns < 0:
            raise ReplayError(f"line {lineno}: recv_ns must be a non-negative integer")
        venue = record.get("venue", "kraken")
        if isinstance(venue, bool) or not isinstance(venue, str) or venue not in _VALID_VENUES:
            raise ReplayError(f"line {lineno}: unsupported venue {venue!r}")
        yield recv_ns, json.dumps(record["msg"]), venue


def read_calibration(path: Path) -> dict[int, tuple[Bar, ...]]:
    bars: dict[int, tuple[Bar, ...]] = {}
    for lineno, record in _records(path):
        if not _is_ohlc(record):
            continue
        interval = record.get("interval")
        if isinstance(interval, bool) or interval not in SUPPORTED_INTERVALS:
            raise ReplayError(f"line {lineno}: unsupported OHLC interval")
        try:
            bars[interval] = parse_ohlc(json.dumps(record.get("data")))
        except KrakenMessageError as exc:
            raise ReplayError(f"line {lineno}: invalid OHLC data: {exc}") from exc
    return bars


def run_replay(runner: ExecutionRunner, records: Iterable[tuple[int, str, Venue]]) -> None:
    last_ns = 0
    for recv_ns, raw, venue in records:
        if recv_ns < last_ns:
            raise ReplayError("replay timestamps must be non-decreasing")
        last_ns = recv_ns
        if venue not in runner.venues:
            continue
        runner.on_message(raw, recv_ns, venue)
        if runner.is_done():
            return
