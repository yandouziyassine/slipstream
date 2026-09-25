from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from websockets.asyncio.client import connect

from slipstream.coinbase import COINBASE_WS_URL, CoinbaseStream, subscribe_messages
from slipstream.coinbase import MAX_MESSAGE_BYTES as COINBASE_MAX_BYTES
from slipstream.kraken import (
    KRAKEN_WS_URL,
    MAX_MESSAGE_BYTES,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken_rest import parse_ohlc
from slipstream.live import root_cause, wall_clock
from slipstream.models import Venue

IDLE_TIMEOUT_S = 30.0

_Validator = Callable[[str | bytes], object]


class RecordError(RuntimeError):
    pass


def open_new_file(path: Path) -> TextIO:
    try:
        return path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RecordError(f"{path} already exists; refusing to overwrite") from exc


def write_ohlc_header(handle: TextIO, interval_min: int, raw: bytes) -> None:
    parse_ohlc(raw)
    record = {"kind": "ohlc", "interval": interval_min, "data": json.loads(raw)}
    handle.write(json.dumps(record) + "\n")


def _subscriptions(venue: Venue, symbol: str, depth: int) -> list[str]:
    if venue == "coinbase":
        return subscribe_messages(symbol)
    return [subscribe_message(symbol, depth), subscribe_trades_message(symbol)]


def _validator(venue: Venue, symbol: str, depth: int) -> _Validator:
    if venue == "coinbase":
        return CoinbaseStream(symbol, depth).parse
    return parse_message


async def record_stream(
    handle: TextIO,
    symbol: str,
    depth: int,
    duration_s: float,
    venues: Sequence[Venue] = ("kraken",),
    urls: Mapping[Venue, str] | None = None,
    clock: Callable[[], int] | None = None,
) -> int:
    endpoints: dict[Venue, str] = {"kraken": KRAKEN_WS_URL, "coinbase": COINBASE_WS_URL}
    endpoints.update(urls or {})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_s
    now = clock or wall_clock()
    written = 0

    def write(venue: Venue, raw: str | bytes) -> None:
        nonlocal written
        record = {"recv_ns": now(), "venue": venue, "msg": json.loads(raw)}
        handle.write(json.dumps(record) + "\n")
        written += 1

    async def feed(venue: Venue) -> None:
        max_size = COINBASE_MAX_BYTES if venue == "coinbase" else MAX_MESSAGE_BYTES
        validate = _validator(venue, symbol, depth)
        async with connect(endpoints[venue], max_size=max_size, open_timeout=10) as ws:
            for message in _subscriptions(venue, symbol, depth):
                await ws.send(message)
            while (remaining := deadline - loop.time()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(IDLE_TIMEOUT_S, remaining))
                except TimeoutError:
                    if loop.time() >= deadline:
                        return
                    raise RecordError(f"no market data from {venue} (idle timeout)") from None
                validate(raw)
                write(venue, raw)

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                group.create_task(feed(venue))
    except ExceptionGroup as errors:
        raise root_cause(errors, RecordError) from errors
    return written
