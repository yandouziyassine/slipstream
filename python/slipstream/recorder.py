from __future__ import annotations

import asyncio
import json
import queue
import threading
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
_WRITE_QUEUE_MAXSIZE = 1024

_Validator = Callable[[str | bytes], object]
_WriteItem = tuple[Venue, int, str]


class RecordError(RuntimeError):
    pass


def _decode(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecordError("received non-UTF-8 message") from exc
    return raw


def _encode(venue: Venue, recv_ns: int, raw: str) -> str:
    if "\n" not in raw and "\r" not in raw:
        return f'{{"recv_ns": {recv_ns}, "venue": "{venue}", "msg": {raw}}}'
    return json.dumps({"recv_ns": recv_ns, "venue": venue, "msg": json.loads(raw)})


class _Writer:
    """Serializes writes to `handle` on a dedicated thread, off the event loop."""

    def __init__(self, handle: TextIO) -> None:
        self._queue: queue.Queue[_WriteItem | None] = queue.Queue(maxsize=_WRITE_QUEUE_MAXSIZE)
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, args=(handle,), daemon=True)
        self._thread.start()

    def _run(self, handle: TextIO) -> None:
        error: Exception | None = None
        while True:
            item = self._queue.get()
            if item is None:
                break
            if error is not None:
                continue
            venue, recv_ns, raw = item
            try:
                handle.write(_encode(venue, recv_ns, raw))
                handle.write("\n")
            except OSError as exc:
                error = exc
        self._error = error

    async def put(self, venue: Venue, recv_ns: int, raw: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._queue.put, (venue, recv_ns, raw))

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()
        if self._error is not None:
            raise RecordError(f"failed to write recording: {self._error}") from self._error


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
    now = clock or wall_clock()
    writer = _Writer(handle)
    written = 0

    delivered: dict[Venue, bool] = dict.fromkeys(venues, False)
    barrier = asyncio.Event()
    deadline = 0.0

    def mark_delivered(venue: Venue) -> None:
        nonlocal deadline
        delivered[venue] = True
        if not barrier.is_set() and all(delivered.values()):
            deadline = loop.time() + duration_s
            barrier.set()

    async def feed(venue: Venue) -> None:
        nonlocal written
        max_size = COINBASE_MAX_BYTES if venue == "coinbase" else MAX_MESSAGE_BYTES
        validate = _validator(venue, symbol, depth)
        async with connect(endpoints[venue], max_size=max_size, open_timeout=10) as ws:
            for message in _subscriptions(venue, symbol, depth):
                await ws.send(message)
            while True:
                if barrier.is_set():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return
                    timeout = min(IDLE_TIMEOUT_S, remaining)
                else:
                    timeout = IDLE_TIMEOUT_S
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except TimeoutError:
                    if barrier.is_set() and loop.time() >= deadline:
                        return
                    raise RecordError(f"no market data from {venue} (idle timeout)") from None
                recv_ns = now()
                text = _decode(raw)
                validate(text)
                await writer.put(venue, recv_ns, text)
                written += 1
                if not delivered[venue]:
                    mark_delivered(venue)

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                group.create_task(feed(venue))
    except ExceptionGroup as errors:
        raise root_cause(errors, RecordError) from errors
    finally:
        writer.close()
    return written
