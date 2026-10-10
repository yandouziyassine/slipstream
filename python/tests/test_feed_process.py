from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
from collections.abc import Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from slipstream.engine_stream import EngineChannel
from slipstream.feed_process import FeedProcessError, FeedSupervisor, market_event
from slipstream.live import LiveFeedError
from slipstream.models import BookUpdate, MarketDataError, TradeBatch, Venue
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_ws import FeedReconnect, ReconnectPolicy

_Handler = Callable[[ServerConnection], Awaitable[None]]

KRAKEN_ACK = json.dumps({"method": "subscribe", "success": True, "result": {}})
# CRC32 of "1010" "10" (ask 101.0 x 1.0) then "990" "10" (bid 99.0 x 1.0), per Kraken's v2 rules.
KRAKEN_SNAPSHOT_CHECKSUM = 3112449789
KRAKEN_SNAPSHOT = json.dumps(
    {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 99.0, "qty": 1.0}],
                "asks": [{"price": 101.0, "qty": 1.0}],
                "checksum": KRAKEN_SNAPSHOT_CHECKSUM,
            }
        ],
    }
)
KRAKEN_TRADE = json.dumps(
    {
        "channel": "trade",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "side": "buy", "price": 100.0, "qty": 0.1}],
    }
)
KRAKEN_HEARTBEAT = json.dumps({"channel": "heartbeat"})
FAST = ReconnectPolicy(base_delay_s=0.01, max_delay_s=0.01)


def coinbase_message(channel: str, seq: int, events: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "channel": channel,
            "client_id": "",
            "timestamp": "2026-09-26T00:00:00Z",
            "sequence_num": seq,
            "events": events,
        }
    )


def coinbase_snapshot(seq: int) -> str:
    updates = [
        {"side": "bid", "event_time": "t", "price_level": "98", "new_quantity": "2"},
        {"side": "offer", "event_time": "t", "price_level": "102", "new_quantity": "2"},
    ]
    return coinbase_message(
        "l2_data", seq, [{"type": "snapshot", "product_id": "BTC-USD", "updates": updates}]
    )


def coinbase_heartbeat(seq: int) -> str:
    return coinbase_message("heartbeats", seq, [{"current_time": "t", "heartbeat_counter": seq}])


async def kraken_feed(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)
    await ws.send(KRAKEN_SNAPSHOT)
    await ws.send(KRAKEN_TRADE)
    try:
        while True:
            await asyncio.sleep(0.05)
            await ws.send(KRAKEN_HEARTBEAT)
    except ConnectionClosed:
        pass


async def coinbase_feed(ws: ServerConnection) -> None:
    for _ in range(3):
        await ws.recv()
    await ws.send(coinbase_message("subscriptions", 0, [{"subscriptions": {}}]))
    await ws.send(coinbase_snapshot(1))
    seq = 2
    try:
        while True:
            await asyncio.sleep(0.05)
            await ws.send(coinbase_heartbeat(seq))
            seq += 1
    except ConnectionClosed:
        pass


async def kraken_bad_json(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)
    await ws.send("not json")
    await ws.wait_closed()


async def kraken_bad_checksum(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)
    await ws.send(KRAKEN_SNAPSHOT.replace(str(KRAKEN_SNAPSHOT_CHECKSUM), "1"))
    await ws.wait_closed()


async def silent_feed(ws: ServerConnection) -> None:
    await ws.wait_closed()


def port_of(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


async def wait_for_status(
    address: str, done: Callable[[pb.StatusReply], bool], timeout_s: float = 30.0
) -> pb.StatusReply:
    channel = EngineChannel(address)
    try:
        await channel.wait_ready(10.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            status = await channel.status()
            if done(status) or loop.time() >= deadline:
                return status
            await asyncio.sleep(0.05)
    finally:
        await channel.close()


def venues_ready(venues: set[str]) -> Callable[[pb.StatusReply], bool]:
    def done(status: pb.StatusReply) -> bool:
        ready = {info.name for info in status.venues if info.has_book and info.fresh}
        return ready >= venues

    return done


def our_feed_processes() -> list[multiprocessing.process.BaseProcess]:
    return [proc for proc in multiprocessing.active_children() if proc.name.startswith("feed-")]


async def wait_for_exit(supervisor: FeedSupervisor, timeout_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while None in supervisor.exit_codes().values() and loop.time() < deadline:
        await asyncio.sleep(0.02)


async def expect_feed_error(supervisor: FeedSupervisor) -> FeedProcessError:
    with pytest.raises(FeedProcessError) as caught:
        await asyncio.wait_for(supervisor.wait_first_error(), timeout=30.0)
    return caught.value


def test_market_event_maps_book_trades_and_heartbeats() -> None:
    book = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 1.0),), "kraken")
    event = market_event(book, "kraken", "BTC/USD")
    assert event is not None and event.WhichOneof("event") == "book"
    assert event.book.recv_ns == 0
    trades = TradeBatch("BTC/USD", False, ((100.0, 0.1),), "kraken")
    event = market_event(trades, "kraken", "BTC/USD")
    assert event is not None and event.WhichOneof("event") == "trades"
    assert event.trades.recv_ns == 0
    event = market_event(None, "kraken", "BTC/USD")
    assert event is not None and event.WhichOneof("event") == "heartbeat"
    assert (event.heartbeat.venue, event.heartbeat.recv_ns) == ("kraken", 0)


def test_market_event_skips_historical_trade_snapshots() -> None:
    snapshot = TradeBatch("BTC/USD", True, ((100.0, 0.1),), "kraken")
    assert market_event(snapshot, "kraken", "BTC/USD") is None


def test_market_event_rejects_another_symbol_or_venue() -> None:
    other_symbol = BookUpdate("ETH/USD", True, ((99.0, 1.0),), ((101.0, 1.0),), "kraken")
    with pytest.raises(MarketDataError, match="symbol"):
        market_event(other_symbol, "kraken", "BTC/USD")
    other_venue = TradeBatch("BTC/USD", False, ((100.0, 0.1),), "coinbase")
    with pytest.raises(MarketDataError, match="venue"):
        market_event(other_venue, "kraken", "BTC/USD")


def test_supervisor_rejects_bad_configuration() -> None:
    with pytest.raises(ValueError, match="venue"):
        FeedSupervisor((), "BTC/USD", 10, "127.0.0.1:1")
    with pytest.raises(ValueError, match="duplicate"):
        FeedSupervisor(("kraken", "kraken"), "BTC/USD", 10, "127.0.0.1:1")
    with pytest.raises(ValueError, match="loopback"):
        FeedSupervisor(("kraken",), "BTC/USD", 10, "10.0.0.1:1")
    with pytest.raises(ValueError, match="unknown venue"):
        FeedSupervisor(("kraken", "binance"), "BTC/USD", 10, "127.0.0.1:1")  # type: ignore[arg-type]


def test_two_feed_processes_stream_concurrently(two_venue_live_engine_address: str) -> None:
    async def scenario() -> None:
        async with serve(kraken_feed, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_feed, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                supervisor = FeedSupervisor(
                    ("kraken", "coinbase"),
                    "BTC/USD",
                    10,
                    two_venue_live_engine_address,
                    urls=urls,
                )
                supervisor.start()
                errors = asyncio.ensure_future(supervisor.wait_first_error())
                try:
                    status = await wait_for_status(
                        two_venue_live_engine_address, venues_ready({"kraken", "coinbase"})
                    )
                    assert not errors.done()
                finally:
                    errors.cancel()
                    await asyncio.gather(errors, return_exceptions=True)
                    await supervisor.stop()
        assert venues_ready({"kraken", "coinbase"})(status)
        books = {book.venue: book for book in status.books}
        assert [(level.price, level.qty) for level in books["kraken"].bids] == [(99.0, 1.0)]
        assert [(level.price, level.qty) for level in books["coinbase"].asks] == [(102.0, 2.0)]
        assert supervisor.exit_codes() == {"kraken": 0, "coinbase": 0}
        assert our_feed_processes() == []

    asyncio.run(scenario())


def test_parser_error_is_reported_with_its_venue_and_message(live_engine_address: str) -> None:
    async def scenario() -> None:
        async with serve(kraken_bad_json, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
            )
            supervisor.start()
            try:
                error = await expect_feed_error(supervisor)
                await wait_for_exit(supervisor)
            finally:
                await supervisor.stop()
        assert isinstance(error, LiveFeedError)
        assert error.venue == "kraken"
        assert error.error_type == "KrakenMessageError"
        assert "invalid JSON" in str(error)
        assert supervisor.exit_codes() == {"kraken": 1}

    asyncio.run(scenario())


def test_repeated_book_checksum_mismatches_stop_the_feed_naming_venue_and_symbol(
    live_engine_address: str,
) -> None:
    async def scenario() -> None:
        async with serve(kraken_bad_checksum, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                reconnect=FAST,
            )
            supervisor.start()
            try:
                error = await expect_feed_error(supervisor)
                await wait_for_exit(supervisor)
            finally:
                await supervisor.stop()
        assert error.venue == "kraken"
        assert "gave up after 5 reconnects" in str(error)
        assert "BookChecksumError: kraken BTC/USD book checksum mismatch" in str(error)
        assert supervisor.exit_codes() == {"kraken": 1}

    asyncio.run(scenario())


def test_idle_feed_is_reported(live_engine_address: str) -> None:
    async def scenario() -> None:
        async with serve(silent_feed, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                idle_timeout_s=0.2,
                reconnect=ReconnectPolicy(max_reconnects=1, base_delay_s=0.01, max_delay_s=0.01),
            )
            supervisor.start()
            try:
                error = await expect_feed_error(supervisor)
            finally:
                await supervisor.stop()
        assert error.venue == "kraken"
        assert "idle timeout" in str(error)

    asyncio.run(scenario())


def test_unreachable_market_data_is_reported(live_engine_address: str) -> None:
    async def scenario() -> None:
        reports: list[FeedReconnect] = []
        supervisor = FeedSupervisor(
            ("kraken",),
            "BTC/USD",
            10,
            live_engine_address,
            urls={"kraken": "ws://127.0.0.1:1"},
            reconnect=FAST,
            on_reconnect=reports.append,
        )
        supervisor.start()
        try:
            error = await expect_feed_error(supervisor)
        finally:
            await supervisor.stop()
        assert error.venue == "kraken"
        assert "gave up after 5 reconnects" in str(error)
        assert [(r.attempt, r.recovered) for r in reports] == [(n, False) for n in range(1, 6)]

    asyncio.run(scenario())


def test_feed_process_that_dies_without_reporting_is_detected(live_engine_address: str) -> None:
    async def scenario() -> None:
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
                (feed,) = our_feed_processes()
                assert feed.pid is not None
                os.kill(feed.pid, signal.SIGKILL)
                error = await expect_feed_error(supervisor)
            finally:
                await supervisor.stop()
        assert error.venue == "kraken"
        assert "-9" in str(error)

    asyncio.run(scenario())


def test_stop_leaves_no_child_processes(live_engine_address: str) -> None:
    async def scenario() -> None:
        connected = asyncio.Event()

        async def handler(ws: ServerConnection) -> None:
            connected.set()
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            supervisor = FeedSupervisor(
                ("kraken",),
                "BTC/USD",
                10,
                live_engine_address,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
            )
            supervisor.start()
            try:
                await asyncio.wait_for(connected.wait(), timeout=30.0)
            finally:
                await supervisor.stop()
        assert our_feed_processes() == []
        assert supervisor.exit_codes() == {"kraken": 0}

    asyncio.run(scenario())
