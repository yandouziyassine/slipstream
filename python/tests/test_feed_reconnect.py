from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from test_feed_process import (
    FAST,
    KRAKEN_ACK,
    KRAKEN_HEARTBEAT,
    KRAKEN_SNAPSHOT,
    kraken_bad_checksum,
    kraken_bad_json,
    kraken_feed,
    port_of,
    silent_feed,
    venues_ready,
    wait_for_status,
)
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from slipstream.feed_process import FeedSupervisor, stream_venue
from slipstream.kraken import KrakenMessageError
from slipstream.live import LiveFeedError
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_ws import FeedReconnect, ReconnectPolicy, ReconnectTracker

_Handler = Callable[[ServerConnection], Awaitable[None]]

KRAKEN_UPDATE = json.dumps(
    {
        "channel": "book",
        "type": "update",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 99.5, "qty": 1.0}],
                "asks": [],
                "checksum": 1,
            }
        ],
    }
)
# The reconnect loop runs in-process here: fake WebSocket servers, a sink standing in for the
# engine stream, and an injected sleep, so backoff timings are checked without waiting for them.


class _Sink:
    def __init__(self) -> None:
        self.events: list[pb.MarketEvent] = []

    def send_nowait(self, event: pb.MarketEvent) -> None:
        self.events.append(event)

    def books(self) -> list[str]:
        """Each book event in order: 'book' with levels, 'empty' when it clears the venue."""
        return [
            "book" if event.book.bids or event.book.asks else "empty"
            for event in self.events
            if event.WhichOneof("event") == "book"
        ]


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _connections(*behaviours: _Handler) -> _Handler:
    """Connection n runs behaviours[n]; later connections repeat the last one."""
    count = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal count
        behaviour = behaviours[min(count, len(behaviours) - 1)]
        count += 1
        await behaviour(ws)

    return handler


async def _subscribed(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)


async def _snapshot_and_heartbeats(ws: ServerConnection) -> None:
    await ws.send(KRAKEN_SNAPSHOT)
    try:
        while True:
            await asyncio.sleep(0.05)
            await ws.send(KRAKEN_HEARTBEAT)
    except ConnectionClosed:
        pass


async def snapshot_then_drop(ws: ServerConnection) -> None:
    await _subscribed(ws)
    await ws.send(KRAKEN_SNAPSHOT)
    await ws.close()


async def update_before_snapshot(ws: ServerConnection) -> None:
    await _subscribed(ws)
    await ws.send(KRAKEN_UPDATE)
    await ws.wait_closed()


async def drop_at_once(ws: ServerConnection) -> None:
    await ws.close()


class _Run:
    def __init__(self) -> None:
        self.sink = _Sink()
        self.sleeps = _Sleeps()
        self.reports: list[FeedReconnect] = []
        self.tracker = ReconnectTracker(
            "kraken", ReconnectPolicy(), self.reports.append, rng=lambda: 1.0
        )

    def _start(self, url: str, idle_timeout_s: float) -> asyncio.Future[None]:
        return asyncio.ensure_future(
            stream_venue(
                "kraken", "BTC/USD", 10, url, self.sink, self.tracker, idle_timeout_s, self.sleeps
            )
        )

    async def until(
        self, url: str, done: Callable[[_Sink], bool], idle_timeout_s: float = 30.0
    ) -> None:
        task = self._start(url, idle_timeout_s)
        try:
            for _ in range(500):
                if done(self.sink) or task.done():
                    break
                await asyncio.sleep(0.01)
            if task.done():
                task.result()
            assert done(self.sink)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.tracker.close()

    async def failure(self, url: str) -> BaseException:
        task = self._start(url, 30.0)
        done, _ = await asyncio.wait({task}, timeout=10.0)
        assert task in done
        error = task.exception()
        assert error is not None
        return error


def _has_book(sink: _Sink) -> bool:
    return "book" in sink.books()


def test_a_dropped_feed_reconnects_clears_the_book_then_continues_from_a_new_snapshot() -> None:
    async def scenario() -> _Run:
        run = _Run()
        async with serve(_connections(snapshot_then_drop, kraken_feed), "127.0.0.1", 0) as server:
            await run.until(
                f"ws://127.0.0.1:{port_of(server)}", lambda s: s.books().count("book") >= 2
            )
        return run

    run = asyncio.run(scenario())
    assert run.sink.books()[:3] == ["book", "empty", "book"]
    assert run.sleeps.delays == [0.5]
    (report,) = run.reports
    assert (report.venue, report.attempt, report.recovered) == ("kraken", 1, True)
    assert report.reason.startswith("ConnectionClosed")
    assert report.downtime_s >= 0


def test_the_sixth_failure_ends_the_feed_after_backing_off_five_times() -> None:
    async def scenario() -> tuple[_Run, BaseException]:
        run = _Run()
        async with serve(drop_at_once, "127.0.0.1", 0) as server:
            error = await run.failure(f"ws://127.0.0.1:{port_of(server)}")
        return run, error

    run, error = asyncio.run(scenario())
    assert isinstance(error, LiveFeedError)
    assert "kraken feed gave up after 5 reconnects" in str(error)
    assert run.sleeps.delays == [0.5, 1, 2, 4, 8]
    assert [(r.attempt, r.recovered) for r in run.reports] == [(n, False) for n in range(1, 6)]
    assert run.sink.books() == ["empty"] * 6


def test_an_update_before_the_new_snapshot_is_never_forwarded() -> None:
    async def scenario() -> tuple[_Run, BaseException]:
        run = _Run()
        handler = _connections(snapshot_then_drop, update_before_snapshot)
        async with serve(handler, "127.0.0.1", 0) as server:
            error = await run.failure(f"ws://127.0.0.1:{port_of(server)}")
        return run, error

    run, error = asyncio.run(scenario())
    assert isinstance(error, KrakenMessageError)
    assert "before the snapshot" in str(error)
    assert run.sink.books() == ["book", "empty"]


def test_a_checksum_mismatch_reconnects_and_counts() -> None:
    async def scenario() -> _Run:
        run = _Run()
        handler = _connections(kraken_bad_checksum, kraken_feed)
        async with serve(handler, "127.0.0.1", 0) as server:
            await run.until(f"ws://127.0.0.1:{port_of(server)}", _has_book)
        return run

    run = asyncio.run(scenario())
    assert run.sink.books()[:2] == ["empty", "book"]
    (report,) = run.reports
    assert report.reason.startswith("BookChecksumError: kraken BTC/USD book checksum mismatch")
    assert report.recovered


def test_an_idle_connection_is_replaced() -> None:
    async def scenario() -> _Run:
        run = _Run()
        async with serve(_connections(silent_feed, kraken_feed), "127.0.0.1", 0) as server:
            await run.until(f"ws://127.0.0.1:{port_of(server)}", _has_book, idle_timeout_s=0.2)
        return run

    run = asyncio.run(scenario())
    (report,) = run.reports
    assert report.reason == "IdleTimeoutError: no market data from kraken (idle timeout)"
    assert report.recovered


def test_malformed_data_is_never_retried() -> None:
    async def scenario() -> tuple[_Run, BaseException]:
        run = _Run()
        async with serve(kraken_bad_json, "127.0.0.1", 0) as server:
            error = await run.failure(f"ws://127.0.0.1:{port_of(server)}")
        return run, error

    run, error = asyncio.run(scenario())
    assert isinstance(error, KrakenMessageError)
    assert run.sleeps.delays == []
    assert run.reports == []


def _kraken_book_empty(status: pb.StatusReply) -> bool:
    return any(info.name == "kraken" and not info.has_book for info in status.venues)


def test_supervised_feed_empties_the_engine_book_while_down_and_reports_the_reconnect(
    live_engine_address: str,
) -> None:
    async def scenario() -> tuple[list[FeedReconnect], bool, pb.StatusReply]:
        release = asyncio.Event()

        async def snapshot_when_released(ws: ServerConnection) -> None:
            await _subscribed(ws)
            await release.wait()
            await _snapshot_and_heartbeats(ws)

        reports: list[FeedReconnect] = []
        handler = _connections(snapshot_then_drop, snapshot_when_released)
        async with serve(handler, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                reconnect=FAST,
                on_reconnect=reports.append,
            )
            supervisor.start()
            errors = asyncio.ensure_future(supervisor.wait_first_error())
            try:
                emptied = _kraken_book_empty(
                    await wait_for_status(live_engine_address, _kraken_book_empty)
                )
                release.set()
                ready = await wait_for_status(live_engine_address, venues_ready({"kraken"}))
                assert not errors.done()
            finally:
                errors.cancel()
                await asyncio.gather(errors, return_exceptions=True)
                await supervisor.stop()
        return reports, emptied, ready

    reports, emptied, ready = asyncio.run(scenario())
    assert emptied
    assert venues_ready({"kraken"})(ready)
    assert [(r.venue, r.attempt, r.recovered) for r in reports] == [("kraken", 1, True)]


def test_a_stopping_feed_empties_its_engine_book(live_engine_address: str) -> None:
    async def scenario() -> pb.StatusReply:
        async with serve(kraken_feed, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
            )
            supervisor.start()
            try:
                await wait_for_status(live_engine_address, venues_ready({"kraken"}))
            finally:
                await supervisor.stop()
        return await wait_for_status(live_engine_address, _kraken_book_empty, timeout_s=5.0)

    assert _kraken_book_empty(asyncio.run(scenario()))


def test_the_supervisor_rejects_malformed_reconnect_reports() -> None:
    supervisor = FeedSupervisor(("kraken",), "BTC/USD", 10, "127.0.0.1:1")
    good = ("kraken", 1, "OSError: down", 1.5, True)
    assert supervisor._to_reconnect(good) == FeedReconnect("kraken", 1, "OSError: down", 1.5, True)
    for bad in [
        ("coinbase", 1, "x", 1.0, True),
        ("kraken", 0, "x", 1.0, True),
        ("kraken", 6, "x", 1.0, True),
        ("kraken", True, "x", 1.0, True),
        ("kraken", 1, 5, 1.0, True),
        ("kraken", 1, "x" * 201, 1.0, True),
        ("kraken", 1, "x", -1.0, True),
        ("kraken", 1, "x", float("nan"), True),
        ("kraken", 1, "x", 1.0, 1),
        ("kraken", 1, "x", 1.0),
        "nonsense",
    ]:
        assert supervisor._to_reconnect(bad) is None, bad
