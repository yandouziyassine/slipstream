from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from slipstream.book import LocalBook, consolidated_book
from slipstream.calibration import CalibrationData, schedule_params
from slipstream.coinbase import CoinbaseStream
from slipstream.engine_stream import (
    EngineBackpressureError,
    EngineChannel,
    EngineError,
    MarketStreamWriter,
    Subscription,
    book_event,
    heartbeat_event,
    tick_event,
    trade_event,
)
from slipstream.feed_process import FeedSupervisor
from slipstream.kraken import parse_message
from slipstream.live import LiveFeedError
from slipstream.models import (
    VENUES,
    BookUpdate,
    Fill,
    MarketDataError,
    OrderSpec,
    TradeBatch,
    Venue,
)
from slipstream.replay import ReplayError
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_ws import IDLE_TIMEOUT_S

_NS_PER_S = 1_000_000_000
# The engine rejects a replay clock that jumps more than one day in a single event.
_MAX_TICK_JUMP_NS = 86_400 * _NS_PER_S
_POLL_S = 0.005
_CATCH_UP_TIMEOUT_S = 10.0
_SEND_TIMEOUT_S = 10.0
_CLOSE_TIMEOUT_S = 30.0
_TERMINAL_TIMEOUT_S = 10.0
_SUBSCRIBE_TIMEOUT_S = 10.0
_TERMINAL_STATES = frozenset({pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED})
_CLOCK_FLAGS: dict[int, str] = {pb.CLOCK_MODE_LIVE: "live", pb.CLOCK_MODE_REPLAY: "replay"}
_VALID_VENUES: frozenset[Venue] = frozenset(VENUES)
# Poll interval and bound while waiting for every venue's book to become fresh in live mode.
_BOOK_POLL_S = 0.05
_BOOK_TIMEOUT_S = 45.0
# Extra time allowed, past an order's own duration, for a live run's terminal updates to arrive.
DEADLINE_GRACE_S = 60

_Parser = Callable[[str | bytes], BookUpdate | TradeBatch | None]


class OrderRejectedError(RuntimeError):
    pass


def clock_mode_mismatch(actual: int, expected: int) -> str | None:
    if actual == expected:
        return None
    wanted = _CLOCK_FLAGS[expected]
    running = f"--clock {_CLOCK_FLAGS[actual]}" if actual in _CLOCK_FLAGS else "no clock mode"
    verb = "runs with" if actual in _CLOCK_FLAGS else "reports"
    return f"engine {verb} {running} but {wanted} needs --clock {wanted}"


async def check_clock_mode(channel: EngineChannel, expected: int) -> None:
    message = clock_mode_mismatch((await channel.status()).clock_mode, expected)
    if message is not None:
        raise EngineError(message)


@dataclass(frozen=True)
class SessionResult:
    statuses: list[pb.OrderStatus]
    fills: list[Fill]
    stats: pb.EngineStats


class ReplaySession:
    """Streams one replay file into a --clock replay engine and executes the orders in it.

    fills holds every fill received so far, so a caller can still report them after a failure.
    """

    def __init__(
        self,
        channel: EngineChannel,
        specs: Sequence[OrderSpec],
        symbol: str,
        logger: logging.Logger,
        calibration: CalibrationData | None = None,
        venues: Sequence[Venue] = ("kraken",),
        book_depth: int = 10,
        fee_bps: Mapping[Venue, float] | None = None,
    ) -> None:
        if not venues:
            raise ValueError("venues must not be empty")
        if len(set(venues)) != len(venues):
            raise ValueError(f"duplicate venue in {venues!r}")
        unknown = [venue for venue in venues if venue not in _VALID_VENUES]
        if unknown:
            raise ValueError(f"unknown venue(s) {unknown!r}")
        self._channel = channel
        self._specs = tuple(specs)
        self._order_ids = frozenset(spec.order_id for spec in self._specs)
        self._symbol = symbol
        self._log = logger
        self._calibration = calibration
        self._venues: frozenset[Venue] = frozenset(venues)
        self._fee_bps: dict[Venue, float] = dict(fee_bps or {})
        self._books: dict[Venue, LocalBook] = {venue: LocalBook(book_depth) for venue in venues}
        self._parsers: dict[Venue, _Parser] = {}
        if "kraken" in self._venues:
            self._parsers["kraken"] = parse_message
        if "coinbase" in self._venues:
            self._parsers["coinbase"] = CoinbaseStream(symbol, book_depth).parse
        self._snapshot_venues: set[Venue] = set()
        self._submitted = False
        self._start_ns = 0
        self._engine_ns = 0
        self._sent = 0
        self._terminal: set[str] = set()
        self.fills: list[Fill] = []

    async def order_statuses(self) -> list[pb.OrderStatus]:
        return self._ours(await self._channel.status())

    async def run(self, records: Iterable[tuple[int, str, Venue]]) -> SessionResult:
        baseline = (await self._channel.status()).stats.events
        subscription = await asyncio.wait_for(
            Subscription.open(self._channel), timeout=_SUBSCRIBE_TIMEOUT_S
        )
        consumer = asyncio.ensure_future(self._consume(subscription))
        try:
            writer = await MarketStreamWriter.open(self._channel, None)
            try:
                await self._stream(records, writer, consumer, baseline)
            except BaseException as exc:
                await self._abandon(writer, exc)
                raise
            await _bounded(writer.close(), _CLOSE_TIMEOUT_S, "closing the market stream")
            _raise_if_failed(consumer)
            if self._submitted:
                await self._await_terminal(consumer)
            status = await self._channel.status()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await subscription.close()
        return SessionResult(self._ours(status), list(self.fills), status.stats)

    def _ours(self, status: pb.StatusReply) -> list[pb.OrderStatus]:
        by_id = {order.order_id: order for order in status.orders}
        return [by_id[spec.order_id] for spec in self._specs if spec.order_id in by_id]

    def _done(self) -> bool:
        return self._submitted and self._terminal >= self._order_ids

    async def _stream(
        self,
        records: Iterable[tuple[int, str, Venue]],
        writer: MarketStreamWriter,
        consumer: asyncio.Future[None],
        baseline: int,
    ) -> None:
        last_ns = 0
        for recv_ns, raw, venue in records:
            if recv_ns < last_ns:
                raise ReplayError("replay timestamps must be non-decreasing")
            last_ns = recv_ns
            _raise_if_failed(consumer)
            if venue not in self._venues:
                continue
            events, submit_now = self._events(raw, recv_ns, venue)
            for event in events:
                await self._send(writer, event)
            if submit_now:
                await self._catch_up(consumer, baseline)
                await self._submit_all(recv_ns)
            if self._done():
                return
            # Let the stream pump and the subscription consumer run between records.
            await asyncio.sleep(0)
        if self._submitted and not self._done():
            deadline = max(self._start_ns + spec.duration_s * _NS_PER_S for spec in self._specs)
            while self._engine_ns < deadline:
                self._engine_ns = min(deadline, self._engine_ns + _MAX_TICK_JUMP_NS)
                await self._send(writer, tick_event(self._engine_ns))

    def _events(self, raw: str, recv_ns: int, venue: Venue) -> tuple[list[pb.MarketEvent], bool]:
        """The engine events for one record, and whether the orders are due after them.

        Every event carries the record's recv_ns, so each record steps the orders once, then.
        """
        events, due = self._record_events(raw, recv_ns, venue)
        if events:
            self._engine_ns = max(self._engine_ns, recv_ns)
        return events, due

    def _record_events(
        self, raw: str, recv_ns: int, venue: Venue
    ) -> tuple[list[pb.MarketEvent], bool]:
        update = self._parsers[venue](raw)
        if isinstance(update, BookUpdate):
            self._check_symbol(update.symbol)
            if self._submitted:
                return [book_event(update, recv_ns)], False
            self._books[venue].apply(update)
            if update.is_snapshot:
                self._snapshot_venues.add(venue)
            return [book_event(update, recv_ns)], self._snapshot_venues >= self._venues
        if isinstance(update, TradeBatch):
            self._check_symbol(update.symbol)
            if not update.is_snapshot:
                return [trade_event(update, recv_ns)], False
            # Historical prints are not live volume; once orders work, only time moves on.
            return ([tick_event(recv_ns)] if self._submitted else []), False
        return [heartbeat_event(venue, recv_ns)], False

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self._symbol:
            raise MarketDataError(f"unexpected symbol {symbol!r}")

    async def _send(self, writer: MarketStreamWriter, event: pb.MarketEvent) -> None:
        loop = asyncio.get_running_loop()
        give_up = loop.time() + _SEND_TIMEOUT_S
        while True:
            try:
                writer.send_nowait(event)
                break
            except EngineBackpressureError:
                if loop.time() >= give_up:
                    raise
                await asyncio.sleep(_POLL_S)
        self._sent += 1

    async def _catch_up(self, consumer: asyncio.Future[None], baseline: int) -> None:
        # Submit and GetStatus run ahead of queued market events, so wait until the engine has
        # applied every event sent so far; replay has no timer, so the event count is exact.
        loop = asyncio.get_running_loop()
        give_up = loop.time() + _CATCH_UP_TIMEOUT_S
        while True:
            _raise_if_failed(consumer)
            status = await self._channel.status()
            books = {info.name for info in status.venues if info.has_book}
            if books >= self._venues and status.stats.events >= baseline + self._sent:
                return
            if loop.time() >= give_up:
                raise EngineError("engine did not apply the replayed order books in time")
            await asyncio.sleep(_POLL_S)

    async def _submit_all(self, now_ns: int) -> None:
        book = consolidated_book(self._symbol, self._books, self._fee_bps)
        planned = [
            (spec, schedule_params(spec, book, now_ns, self._calibration)) for spec in self._specs
        ]
        self._start_ns = now_ns
        self._submitted = True
        for spec, params in planned:
            accepted, reason = await self._channel.submit(spec, now_ns, params)
            if not accepted:
                raise OrderRejectedError(f"order {spec.order_id} rejected: {reason}")
            self._log.info(
                "order submitted",
                extra={
                    "fields": {
                        "event": "submit",
                        "order_id": spec.order_id,
                        "algo": spec.algo,
                        "side": spec.side,
                        "qty": spec.qty,
                        "duration_s": spec.duration_s,
                        "slices": spec.num_slices,
                        "params": repr(params),
                    }
                },
            )

    async def _consume(self, subscription: Subscription) -> None:
        async for event in subscription:
            kind = event.WhichOneof("event")
            if kind == "fill" and event.fill.order_id in self._order_ids:
                self._record_fill(event.fill)
            elif kind == "order" and event.order.order_id in self._order_ids:
                if event.order.state in _TERMINAL_STATES:
                    self._terminal.add(event.order.order_id)
                    if self._terminal >= self._order_ids:
                        return
        raise EngineError("engine subscription ended before every order finished")

    def _record_fill(self, message: pb.Fill) -> None:
        fill = Fill(
            message.order_id, message.ts_ns, message.qty, message.price, message.venue, message.fee
        )
        self.fills.append(fill)
        self._log.info(
            "fill",
            extra={
                "fields": {
                    "event": "fill",
                    "order_id": fill.order_id,
                    "qty": fill.qty,
                    "price": fill.price,
                    "ts_ns": fill.ts_ns,
                    "venue": fill.venue,
                    "fee": fill.fee,
                }
            },
        )

    async def _await_terminal(self, consumer: asyncio.Future[None]) -> None:
        await asyncio.wait({consumer}, timeout=_TERMINAL_TIMEOUT_S)
        if not consumer.done():
            working = sorted(self._order_ids - self._terminal)
            raise EngineError(f"orders {working} did not finish after the replay ended")
        consumer.result()

    async def _abandon(self, writer: MarketStreamWriter, cause: BaseException) -> None:
        # When the engine itself rejected the stream, that rejection is the root cause of an
        # engine-side failure here (for example a catch-up wait that could never finish). Data
        # and order errors stay the root cause; the close failure is attached as a note.
        try:
            await _bounded(writer.close(), _CLOSE_TIMEOUT_S, "closing the market stream")
        except EngineError as exc:
            if isinstance(cause, EngineError):
                raise exc from cause
            self._log.error(f"market stream error during shutdown: {exc}")
            if isinstance(cause, Exception):
                cause.add_note(f"market stream also failed: {exc}")


def _raise_if_failed(consumer: asyncio.Future[None]) -> None:
    if consumer.done() and not consumer.cancelled():
        consumer.result()


async def _bounded(awaitable: Awaitable[object], timeout_s: float, action: str) -> None:
    try:
        await asyncio.wait_for(awaitable, timeout=timeout_s)
    except TimeoutError as exc:
        raise EngineError(f"timed out {action}") from exc


async def run_replay_session(
    channel: EngineChannel,
    specs: Sequence[OrderSpec],
    symbol: str,
    logger: logging.Logger,
    *,
    records: Iterable[tuple[int, str, Venue]],
    calibration: CalibrationData | None = None,
    venues: Sequence[Venue] = ("kraken",),
    book_depth: int = 10,
    fee_bps: Mapping[Venue, float] | None = None,
) -> SessionResult:
    session = ReplaySession(
        channel, specs, symbol, logger, calibration, venues, book_depth, fee_bps
    )
    return await session.run(records)


def _check_venues(venues: Sequence[Venue]) -> None:
    if not venues:
        raise ValueError("venues must not be empty")
    if len(set(venues)) != len(venues):
        raise ValueError(f"duplicate venue in {venues!r}")
    unknown = [venue for venue in venues if venue not in _VALID_VENUES]
    if unknown:
        raise ValueError(f"unknown venue(s) {unknown!r}")


async def _watch_feeds(supervisor: FeedSupervisor) -> None:
    # wait_first_error() never returns normally, so this coroutine either diverges (an
    # exception propagates) or is cancelled by the caller; its own -> None is never reached.
    await supervisor.wait_first_error()


def _check_running(consumer: asyncio.Future[None], error_watch: asyncio.Future[None]) -> None:
    """Feed errors outrank engine/subscription errors, which outrank a bare timeout."""
    _raise_if_failed(error_watch)
    _raise_if_failed(consumer)


class LiveSession:
    """Streams each configured venue's live feed, one process per venue, into a --clock live
    engine and executes the orders in it.

    fills holds every fill received so far, so a caller can still report them after a failure.
    """

    def __init__(
        self,
        channel: EngineChannel,
        specs: Sequence[OrderSpec],
        symbol: str,
        logger: logging.Logger,
        calibration: CalibrationData | None = None,
        venues: Sequence[Venue] = ("kraken",),
        book_depth: int = 10,
        fee_bps: Mapping[Venue, float] | None = None,
    ) -> None:
        _check_venues(venues)
        self._channel = channel
        self._specs = tuple(specs)
        self._order_ids = frozenset(spec.order_id for spec in self._specs)
        self._symbol = symbol
        self._log = logger
        self._calibration = calibration
        self._venues_seq: tuple[Venue, ...] = tuple(venues)
        self._venues: frozenset[Venue] = frozenset(venues)
        self._book_depth = book_depth
        self._fee_bps: dict[Venue, float] = dict(fee_bps or {})
        self._terminal: set[str] = set()
        self.fills: list[Fill] = []

    async def order_statuses(self) -> list[pb.OrderStatus]:
        return self._ours(await self._channel.status())

    def _ours(self, status: pb.StatusReply) -> list[pb.OrderStatus]:
        by_id = {order.order_id: order for order in status.orders}
        return [by_id[spec.order_id] for spec in self._specs if spec.order_id in by_id]

    async def run(
        self,
        engine_address: str,
        urls: Mapping[Venue, str] | None = None,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        book_timeout_s: float = _BOOK_TIMEOUT_S,
        deadline_grace_s: float = DEADLINE_GRACE_S,
    ) -> SessionResult:
        await check_clock_mode(self._channel, pb.CLOCK_MODE_LIVE)
        # Open the subscription before any submit: the engine delivers fills and order updates
        # only to an active subscriber, so events from before it opens are never seen.
        subscription = await asyncio.wait_for(
            Subscription.open(self._channel), timeout=_SUBSCRIBE_TIMEOUT_S
        )
        consumer = asyncio.ensure_future(self._consume(subscription))
        error_watch: asyncio.Future[None] | None = None
        supervisor: FeedSupervisor | None = None
        try:
            supervisor = FeedSupervisor(
                self._venues_seq,
                self._symbol,
                self._book_depth,
                engine_address,
                urls,
                idle_timeout_s,
            )
            supervisor.start()
            error_watch = asyncio.ensure_future(_watch_feeds(supervisor))
            await self._await_books(consumer, error_watch, book_timeout_s)
            await self._submit_all()
            _check_running(consumer, error_watch)
            deadline_s = max(spec.duration_s for spec in self._specs) + deadline_grace_s
            await self._await_terminal(consumer, error_watch, deadline_s)
            status = await self._channel.status()
        finally:
            if error_watch is not None:
                error_watch.cancel()
                await asyncio.gather(error_watch, return_exceptions=True)
            if supervisor is not None:
                await supervisor.stop()
            await subscription.close()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        return SessionResult(self._ours(status), list(self.fills), status.stats)

    async def _await_books(
        self,
        consumer: asyncio.Future[None],
        error_watch: asyncio.Future[None],
        timeout_s: float,
    ) -> None:
        # stats.events is not a useful signal here: the live timer advances it on its own, with
        # or without any market data, unlike the replay catch-up wait that counts exact events.
        loop = asyncio.get_running_loop()
        give_up = loop.time() + timeout_s
        while True:
            _check_running(consumer, error_watch)
            status = await self._channel.status()
            ready = {info.name for info in status.venues if info.has_book and info.fresh}
            if ready >= self._venues:
                return
            if loop.time() >= give_up:
                missing = sorted(self._venues - ready)
                raise LiveFeedError(f"venue book(s) {missing} not fresh after {timeout_s:.0f}s")
            await asyncio.sleep(_BOOK_POLL_S)

    async def _await_terminal(
        self,
        consumer: asyncio.Future[None],
        error_watch: asyncio.Future[None],
        deadline_s: float,
    ) -> None:
        done, _pending = await asyncio.wait(
            {consumer, error_watch}, timeout=deadline_s, return_when=asyncio.FIRST_COMPLETED
        )
        if error_watch in done:
            error_watch.result()
        if consumer in done:
            consumer.result()
            return
        working = sorted(self._order_ids - self._terminal)
        raise EngineError(f"orders {working} did not finish within {deadline_s:.0f}s")

    def _local_books(self, status: pb.StatusReply) -> dict[Venue, LocalBook]:
        books: dict[Venue, LocalBook] = {
            venue: LocalBook(self._book_depth) for venue in self._venues
        }
        by_venue = {venue_book.venue: venue_book for venue_book in status.books}
        for venue, book in books.items():
            proto_book = by_venue.get(venue)
            if proto_book is None:
                continue
            bids = tuple((level.price, level.qty) for level in proto_book.bids)
            asks = tuple((level.price, level.qty) for level in proto_book.asks)
            book.apply(BookUpdate(self._symbol, True, bids, asks, venue))
        return books

    async def _submit_all(self) -> None:
        status = await self._channel.status()
        books = self._local_books(status)
        book = consolidated_book(self._symbol, books, self._fee_bps)
        now_ns = time.time_ns()
        planned = [
            (spec, schedule_params(spec, book, now_ns, self._calibration)) for spec in self._specs
        ]
        for spec, params in planned:
            accepted, reason = await self._channel.submit(spec, now_ns, params)
            if not accepted:
                raise OrderRejectedError(f"order {spec.order_id} rejected: {reason}")
            self._log.info(
                "order submitted",
                extra={
                    "fields": {
                        "event": "submit",
                        "order_id": spec.order_id,
                        "algo": spec.algo,
                        "side": spec.side,
                        "qty": spec.qty,
                        "duration_s": spec.duration_s,
                        "slices": spec.num_slices,
                        "params": repr(params),
                    }
                },
            )

    async def _consume(self, subscription: Subscription) -> None:
        async for event in subscription:
            kind = event.WhichOneof("event")
            if kind == "fill" and event.fill.order_id in self._order_ids:
                self._record_fill(event.fill)
            elif kind == "order" and event.order.order_id in self._order_ids:
                if event.order.state in _TERMINAL_STATES:
                    self._terminal.add(event.order.order_id)
                    if self._terminal >= self._order_ids:
                        return
        raise EngineError("engine subscription ended before every order finished")

    def _record_fill(self, message: pb.Fill) -> None:
        fill = Fill(
            message.order_id, message.ts_ns, message.qty, message.price, message.venue, message.fee
        )
        self.fills.append(fill)
        self._log.info(
            "fill",
            extra={
                "fields": {
                    "event": "fill",
                    "order_id": fill.order_id,
                    "qty": fill.qty,
                    "price": fill.price,
                    "ts_ns": fill.ts_ns,
                    "venue": fill.venue,
                    "fee": fill.fee,
                }
            },
        )


async def run_live_session(
    channel: EngineChannel,
    specs: Sequence[OrderSpec],
    symbol: str,
    logger: logging.Logger,
    *,
    engine_address: str,
    calibration: CalibrationData | None = None,
    venues: Sequence[Venue] = ("kraken",),
    book_depth: int = 10,
    fee_bps: Mapping[Venue, float] | None = None,
    urls: Mapping[Venue, str] | None = None,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    book_timeout_s: float = _BOOK_TIMEOUT_S,
    deadline_grace_s: float = DEADLINE_GRACE_S,
) -> SessionResult:
    session = LiveSession(channel, specs, symbol, logger, calibration, venues, book_depth, fee_bps)
    return await session.run(engine_address, urls, idle_timeout_s, book_timeout_s, deadline_grace_s)
