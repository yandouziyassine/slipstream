from __future__ import annotations

import asyncio
import math
import multiprocessing
import queue
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from multiprocessing.context import SpawnProcess
from typing import Any, NoReturn, Protocol

from websockets.asyncio.client import connect

from slipstream.config import validate_engine_address
from slipstream.engine_stream import (
    EngineChannel,
    EngineError,
    MarketStreamWriter,
    book_event,
    heartbeat_event,
    trade_event,
)
from slipstream.live import LiveFeedError
from slipstream.models import VENUES, BookUpdate, MarketDataError, TradeBatch, Venue
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_ws import (
    DEFAULT_RECONNECT,
    IDLE_TIMEOUT_S,
    MAX_REASON_CHARS,
    FeedReconnect,
    IdleTimeoutError,
    ReconnectPolicy,
    ReconnectTracker,
    endpoints,
    give_up_message,
    is_reconnectable,
    max_message_bytes,
    parser,
    subscriptions,
)

_MAX_DETAIL_CHARS = 500
_ENGINE_READY_TIMEOUT_S = 10.0
_PARENT_CHECK_INTERVAL_S = 1.0
_POLL_INTERVAL_S = 0.05
# A feed writes its error to the queue before it exits; give the pipe a moment to deliver it.
_UNREPORTED_EXIT_GRACE_S = 0.5
_STOP_TIMEOUT_S = 5.0
_KILL_JOIN_TIMEOUT_S = 2.0
# Disconnects explain less than data or engine errors, as in live.root_cause.
_NETWORK_ERROR_TYPES = frozenset(
    {
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "InvalidHandshake",
        "InvalidMessage",
        "InvalidStatus",
        "OSError",
        "TimeoutError",
        "gaierror",
    }
)

ErrorReport = tuple[str, str, str]
ReconnectReport = tuple[str, int, str, float, bool]


class EventSink(Protocol):
    def send_nowait(self, event: pb.MarketEvent) -> None: ...


class FeedProcessError(LiveFeedError):
    def __init__(self, venue: str, error_type: str, detail: str) -> None:
        super().__init__(f"{venue} feed failed: {error_type}: {detail}")
        self.venue = venue
        self.error_type = error_type
        self.detail = detail


def market_event(
    update: BookUpdate | TradeBatch | None, venue: Venue, symbol: str
) -> pb.MarketEvent | None:
    """The engine event for one parsed message, or None when there is nothing to send."""
    if update is None:
        return heartbeat_event(venue, None)
    if update.symbol != symbol:
        raise MarketDataError(f"unexpected symbol {update.symbol[:32]!r}")
    if update.venue != venue:
        raise MarketDataError(f"update from venue {update.venue!r} on the {venue} feed")
    if isinstance(update, BookUpdate):
        return book_event(update, None)
    if update.is_snapshot:
        # Historical prints must not count as live volume.
        return None
    return trade_event(update, None)


def empty_book_event(venue: Venue, symbol: str) -> pb.MarketEvent:
    """A snapshot with no levels: the engine drops the venue's book until a real one arrives."""
    return book_event(BookUpdate(symbol, True, (), (), venue), None)


async def stream_venue(
    venue: Venue,
    symbol: str,
    depth: int,
    url: str,
    sink: EventSink,
    tracker: ReconnectTracker,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> NoReturn:
    """Stream one venue into sink, reconnecting after network failures until the cap."""
    while True:
        try:
            await _connection(venue, symbol, depth, url, sink, tracker, idle_timeout_s)
        except Exception as exc:
            if not is_reconnectable(exc):
                raise
            delay = tracker.failed(exc)
            # The pre-disconnect book must never be traded on, even for the backoff's duration.
            sink.send_nowait(empty_book_event(venue, symbol))
            if delay is None:
                raise LiveFeedError(
                    give_up_message(venue, tracker.policy.max_reconnects, exc)
                ) from exc
            await sleep(delay)


async def _connection(
    venue: Venue,
    symbol: str,
    depth: int,
    url: str,
    sink: EventSink,
    tracker: ReconnectTracker,
    idle_timeout_s: float,
) -> None:
    parse = parser(venue, symbol, depth)
    async with connect(
        url, max_size=max_message_bytes(venue), open_timeout=10, close_timeout=1
    ) as ws:
        for message in subscriptions(venue, symbol, depth):
            await ws.send(message)
        while True:
            try:
                async with asyncio.timeout(idle_timeout_s):
                    raw = await ws.recv()
            except TimeoutError:
                raise IdleTimeoutError(f"no market data from {venue} (idle timeout)") from None
            update = parse(raw)
            event = market_event(update, venue, symbol)
            if event is not None:
                sink.send_nowait(event)
                tracker.received(update)
                # recv() may return buffered messages without suspending; let the pump write.
                await asyncio.sleep(0)


def run_feed(
    venue: Venue,
    symbol: str,
    depth: int,
    engine_address: str,
    url: str,
    error_queue: multiprocessing.Queue[ErrorReport],
    reconnect_queue: multiprocessing.Queue[ReconnectReport],
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    reconnect: ReconnectPolicy = DEFAULT_RECONNECT,
) -> None:
    """Spawn target: stream one venue into the engine until SIGTERM or a fatal error."""
    # The parent owns shutdown: Ctrl-C reaches the whole process group.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def report(event: FeedReconnect) -> None:
        reconnect_queue.put(
            (event.venue, event.attempt, event.reason, event.downtime_s, event.recovered)
        )

    tracker = ReconnectTracker(venue, reconnect, report)
    try:
        asyncio.run(_run(venue, symbol, depth, engine_address, url, idle_timeout_s, tracker))
    except BaseException as exc:
        error_queue.put((venue, type(exc).__name__, str(exc)[:_MAX_DETAIL_CHARS]))
        error_queue.close()
        error_queue.join_thread()
        sys.exit(1)


class _StopRequest:
    def __init__(self, task: asyncio.Task[Any]) -> None:
        self._task = task
        self.requested = False

    def request(self) -> None:
        self.requested = True
        self._task.cancel()

    def acknowledge(self) -> None:
        self._task.uncancel()


async def _run(
    venue: Venue,
    symbol: str,
    depth: int,
    engine_address: str,
    url: str,
    idle_timeout_s: float,
    tracker: ReconnectTracker,
) -> None:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("feed must run inside an asyncio task")
    stop = _StopRequest(task)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.request)
    watchdog = asyncio.ensure_future(_stop_when_parent_dies(stop))
    try:
        await _stream(venue, symbol, depth, engine_address, url, idle_timeout_s, tracker, stop)
    except asyncio.CancelledError:
        if not stop.requested:
            raise
        stop.acknowledge()
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


async def _stop_when_parent_dies(stop: _StopRequest) -> None:
    parent = multiprocessing.parent_process()
    if parent is None:
        return
    while parent.is_alive():
        await asyncio.sleep(_PARENT_CHECK_INTERVAL_S)
    stop.request()


async def _stream(
    venue: Venue,
    symbol: str,
    depth: int,
    engine_address: str,
    url: str,
    idle_timeout_s: float,
    tracker: ReconnectTracker,
    stop: _StopRequest,
) -> None:
    channel = EngineChannel(engine_address)
    try:
        await channel.wait_ready(_ENGINE_READY_TIMEOUT_S)
        async with await MarketStreamWriter.open(channel, venue) as writer:
            try:
                await stream_venue(venue, symbol, depth, url, writer, tracker, idle_timeout_s)
            except asyncio.CancelledError:
                if not stop.requested:
                    raise
                # SIGTERM: leave the stream context normally so the engine stream closes cleanly.
                stop.acknowledge()
            finally:
                tracker.close()
                _empty_book_quietly(writer, venue, symbol)
    finally:
        await channel.close()


def _empty_book_quietly(writer: MarketStreamWriter, venue: Venue, symbol: str) -> None:
    # A later run on this engine must wait for a new book, not trade on this frozen one. If the
    # engine stream is already broken, its own error is the one reported.
    try:
        writer.send_nowait(empty_book_event(venue, symbol))
    except EngineError:
        pass


class FeedSupervisor:
    """One spawned feed process per venue, all streaming into the same engine."""

    def __init__(
        self,
        venues: Sequence[Venue],
        symbol: str,
        depth: int,
        engine_address: str,
        urls: Mapping[Venue, str] | None = None,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        reconnect: ReconnectPolicy = DEFAULT_RECONNECT,
        on_reconnect: Callable[[FeedReconnect], None] | None = None,
    ) -> None:
        if not venues:
            raise ValueError("at least one venue is required")
        if len(set(venues)) != len(venues):
            raise ValueError(f"duplicate venue in {list(venues)!r}")
        unknown = [venue for venue in venues if venue not in VENUES]
        if unknown:
            raise ValueError(f"unknown venue(s) {unknown!r}")
        validate_engine_address(engine_address)
        self._venues = tuple(venues)
        self._symbol = symbol
        self._depth = depth
        self._engine_address = engine_address
        self._urls = endpoints(urls)
        self._idle_timeout_s = idle_timeout_s
        self._reconnect = reconnect
        self._on_reconnect = on_reconnect
        self._context = multiprocessing.get_context("spawn")
        self._errors: multiprocessing.Queue[ErrorReport] = self._context.Queue()
        self._reconnects: multiprocessing.Queue[ReconnectReport] = self._context.Queue()
        self._processes: dict[Venue, SpawnProcess] = {}
        self._stopping = False

    def start(self) -> None:
        """Spawn every feed. Call stop() afterwards even if this raises part way through."""
        if self._processes:
            raise RuntimeError("feeds already started")
        for venue in self._venues:
            process = self._context.Process(
                target=run_feed,
                args=(
                    venue,
                    self._symbol,
                    self._depth,
                    self._engine_address,
                    self._urls[venue],
                    self._errors,
                    self._reconnects,
                    self._idle_timeout_s,
                    self._reconnect,
                ),
                name=f"feed-{venue}",
                # Daemonic feeds are terminated if the parent exits without calling stop().
                daemon=True,
            )
            process.start()
            self._processes[venue] = process

    async def wait_first_error(self) -> NoReturn:
        """Raise FeedProcessError for the first feed that fails or exits. Cancel to stop waiting."""
        if not self._processes:
            raise RuntimeError("feeds not started")
        loop = asyncio.get_running_loop()
        exited_at: dict[Venue, float] = {}
        while True:
            self._drain_reconnects()
            reports = self._drain_reports()
            if reports:
                raise _root_cause(reports)
            if not self._stopping:
                for venue, process in self._processes.items():
                    if process.exitcode is None:
                        continue
                    first_seen = exited_at.setdefault(venue, loop.time())
                    if loop.time() - first_seen >= _UNREPORTED_EXIT_GRACE_S:
                        raise FeedProcessError(
                            venue,
                            "FeedExited",
                            f"feed process exited with code {process.exitcode} "
                            "without reporting an error",
                        )
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def stop(self) -> None:
        """Terminate every feed, wait up to 5 s for a clean exit, then kill what is left."""
        self._stopping = True
        processes = list(self._processes.values())
        try:
            for process in processes:
                if process.is_alive():
                    process.terminate()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _STOP_TIMEOUT_S
            while any(process.is_alive() for process in processes) and loop.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_S / 2)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                # Bounded: this runs on the event loop, and a killed child is reaped in ms.
                process.join(timeout=_KILL_JOIN_TIMEOUT_S)
            self._drain_reconnects()

    def exit_codes(self) -> dict[Venue, int | None]:
        return {venue: process.exitcode for venue, process in self._processes.items()}

    def _drain_reports(self) -> list[FeedProcessError]:
        reports: list[FeedProcessError] = []
        while True:
            try:
                item = self._errors.get_nowait()
            except queue.Empty:
                return reports
            reports.append(self._to_error(item))

    def _drain_reconnects(self) -> None:
        while True:
            try:
                item = self._reconnects.get_nowait()
            except queue.Empty:
                return
            event = self._to_reconnect(item)
            if event is not None and self._on_reconnect is not None:
                self._on_reconnect(event)

    def _to_reconnect(self, item: object) -> FeedReconnect | None:
        if not (isinstance(item, tuple) and len(item) == 5):
            return None
        venue, attempt, reason, downtime_s, recovered = item
        valid = (
            venue in self._venues
            and isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and 1 <= attempt <= self._reconnect.max_reconnects
            and isinstance(reason, str)
            and len(reason) <= MAX_REASON_CHARS
            and isinstance(downtime_s, float)
            and math.isfinite(downtime_s)
            and downtime_s >= 0
            and isinstance(recovered, bool)
        )
        return FeedReconnect(venue, attempt, reason, downtime_s, recovered) if valid else None

    def _to_error(self, item: object) -> FeedProcessError:
        if (
            isinstance(item, tuple)
            and len(item) == 3
            and all(isinstance(part, str) for part in item)
            and item[0] in self._venues
        ):
            venue, error_type, detail = item
            return FeedProcessError(venue, error_type[:64], detail[:_MAX_DETAIL_CHARS])
        return FeedProcessError("unknown", "InvalidReport", "malformed feed error report")


def _root_cause(reports: list[FeedProcessError]) -> FeedProcessError:
    for report in reports:
        if report.error_type not in _NETWORK_ERROR_TYPES:
            return report
    return reports[0]
