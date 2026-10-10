from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from websockets.asyncio.client import connect

from slipstream.kraken_rest import parse_ohlc
from slipstream.live import root_cause, wall_clock
from slipstream.models import Venue
from slipstream.venue_ws import (
    DEFAULT_RECONNECT,
    IDLE_TIMEOUT_S,
    FeedReconnect,
    IdleTimeoutError,
    ReconnectPolicy,
    ReconnectTracker,
    describe,
    endpoints,
    give_up_message,
    is_reconnectable,
    max_message_bytes,
    parser,
    subscriptions,
)

_WRITE_QUEUE_MAXSIZE = 1024


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


def _marker(venue: Venue, recv_ns: int, attempt: int, reason: str) -> str:
    """Replay discards this venue's book here, until the new connection's snapshot."""
    return json.dumps(
        {
            "recv_ns": recv_ns,
            "venue": venue,
            "kind": "reconnect",
            "attempt": attempt,
            "reason": reason,
        }
    )


class _Writer:
    """Serializes writes to `handle` on a dedicated thread, off the event loop."""

    def __init__(self, handle: TextIO) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_WRITE_QUEUE_MAXSIZE)
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
            try:
                handle.write(item)
                handle.write("\n")
            except OSError as exc:
                error = exc
        self._error = error

    async def put(self, line: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._queue.put, line)

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


async def record_stream(
    handle: TextIO,
    symbol: str,
    depth: int,
    duration_s: float,
    venues: Sequence[Venue] = ("kraken",),
    urls: Mapping[Venue, str] | None = None,
    clock: Callable[[], int] | None = None,
    reconnect: ReconnectPolicy = DEFAULT_RECONNECT,
    on_reconnect: Callable[[FeedReconnect], None] | None = None,
) -> int:
    urls_by_venue = endpoints(urls)
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

    def report(event: FeedReconnect) -> None:
        if on_reconnect is not None:
            on_reconnect(event)

    def finished() -> bool:
        return barrier.is_set() and loop.time() >= deadline

    async def connection(venue: Venue, tracker: ReconnectTracker) -> None:
        nonlocal written
        max_size = max_message_bytes(venue)
        validate = parser(venue, symbol, depth)
        async with connect(urls_by_venue[venue], max_size=max_size, open_timeout=10) as ws:
            for message in subscriptions(venue, symbol, depth):
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
                    if finished():
                        return
                    raise IdleTimeoutError(f"no market data from {venue} (idle timeout)") from None
                recv_ns = now()
                text = _decode(raw)
                update = validate(text)
                await writer.put(_encode(venue, recv_ns, text))
                written += 1
                tracker.received(update)
                if not delivered[venue]:
                    mark_delivered(venue)

    async def feed(venue: Venue) -> None:
        tracker = ReconnectTracker(venue, reconnect, report)
        try:
            while True:
                try:
                    await connection(venue, tracker)
                    return
                except Exception as exc:
                    if not is_reconnectable(exc):
                        raise
                    if finished():
                        return
                    delay = tracker.failed(exc)
                    await writer.put(_marker(venue, now(), tracker.failures, describe(exc)))
                    if delay is None:
                        raise RecordError(
                            give_up_message(venue, reconnect.max_reconnects, exc)
                        ) from exc
                    if barrier.is_set():
                        delay = min(delay, max(0.0, deadline - loop.time()))
                    await asyncio.sleep(delay)
                    if finished():
                        return
        finally:
            tracker.close()

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                group.create_task(feed(venue))
    except ExceptionGroup as errors:
        raise root_cause(errors, RecordError) from errors
    finally:
        writer.close()
    return written
