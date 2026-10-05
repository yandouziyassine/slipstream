from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
from collections.abc import Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from slipstream.engine_stream import EngineChannel
from slipstream.feed_process import FeedProcessError
from slipstream.live import LiveFeedError
from slipstream.models import OrderSpec, Venue
from slipstream.session import LiveSession, SessionResult, run_live_session
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_ws import ReconnectPolicy

_Handler = Callable[[ServerConnection], Awaitable[None]]

KRAKEN_ACK = json.dumps({"method": "subscribe", "success": True, "result": {}})
# Prices stay within the engine's default 50bps deviation collar (mid ~100000, spread ~2bps),
# unlike the wide toy prices in test_feed_process.py, which never need an order to actually fill.
KRAKEN_SNAPSHOT = json.dumps(
    {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 99990.0, "qty": 1.0}],
                "asks": [{"price": 100010.0, "qty": 1.0}],
                "checksum": 2995216371,
            }
        ],
    }
)
KRAKEN_TRADE = json.dumps(
    {
        "channel": "trade",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "side": "buy", "price": 100000.0, "qty": 0.1}],
    }
)
KRAKEN_HEARTBEAT = json.dumps({"channel": "heartbeat"})


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
        {"side": "bid", "event_time": "t", "price_level": "99980", "new_quantity": "2"},
        {"side": "offer", "event_time": "t", "price_level": "100020", "new_quantity": "2"},
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


async def silent_feed(ws: ServerConnection) -> None:
    await ws.wait_closed()


def port_of(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


def our_feed_processes() -> list[multiprocessing.process.BaseProcess]:
    return [proc for proc in multiprocessing.active_children() if proc.name.startswith("feed-")]


def test_live_session_completes_with_fills(two_venue_live_engine_address: str) -> None:
    async def scenario() -> None:
        async with serve(kraken_feed, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_feed, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                channel = EngineChannel(two_venue_live_engine_address)
                try:
                    await channel.wait_ready(5.0)
                    fees = await channel.venue_fees()
                    result = await run_live_session(
                        channel,
                        [OrderSpec("live-ok", "buy", 0.02, 2, 2)],
                        "BTC/USD",
                        logging.getLogger("test"),
                        engine_address=two_venue_live_engine_address,
                        venues=("kraken", "coinbase"),
                        fee_bps=fees,
                        urls=urls,
                        book_timeout_s=10.0,
                    )
                finally:
                    await channel.close()
        assert result.statuses != []
        assert result.statuses[0].state in (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED)
        assert result.fills != []
        assert {fill.venue for fill in result.fills} <= {"kraken", "coinbase"}
        assert all(fill.fee >= 0 for fill in result.fills)
        assert our_feed_processes() == []

    asyncio.run(scenario())


def test_feed_parser_error_ends_session_and_preserves_fills(
    two_venue_live_engine_address: str,
) -> None:
    async def coinbase_then_bad(ws: ServerConnection) -> None:
        for _ in range(3):
            await ws.recv()
        await ws.send(coinbase_message("subscriptions", 0, [{"subscriptions": {}}]))
        await ws.send(coinbase_snapshot(1))
        await asyncio.sleep(0.3)
        await ws.send("not json")
        await ws.wait_closed()

    async def scenario() -> None:
        async with serve(kraken_feed, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_then_bad, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                channel = EngineChannel(two_venue_live_engine_address)
                try:
                    await channel.wait_ready(5.0)
                    fees = await channel.venue_fees()
                    session = LiveSession(
                        channel,
                        [OrderSpec("live-err", "buy", 0.02, 5, 5)],
                        "BTC/USD",
                        logging.getLogger("test"),
                        venues=("kraken", "coinbase"),
                        fee_bps=fees,
                    )
                    with pytest.raises(FeedProcessError) as caught:
                        await session.run(
                            two_venue_live_engine_address, urls=urls, book_timeout_s=10.0
                        )
                    assert caught.value.venue == "coinbase"
                    assert session.fills != []
                finally:
                    await channel.close()
        assert our_feed_processes() == []

    asyncio.run(scenario())


def test_book_wait_timeout_raises_live_feed_error(live_engine_address: str) -> None:
    async def scenario() -> None:
        async with serve(silent_feed, "127.0.0.1", 0) as server:
            urls: dict[Venue, str] = {"kraken": f"ws://127.0.0.1:{port_of(server)}"}
            channel = EngineChannel(live_engine_address)
            try:
                await channel.wait_ready(5.0)
                fees = await channel.venue_fees()
                session = LiveSession(
                    channel,
                    [OrderSpec("live-timeout", "buy", 0.02, 5, 5)],
                    "BTC/USD",
                    logging.getLogger("test"),
                    venues=("kraken",),
                    fee_bps=fees,
                )
                with pytest.raises(LiveFeedError, match="not fresh"):
                    await session.run(live_engine_address, urls=urls, book_timeout_s=0.5)
            finally:
                await channel.close()
        assert our_feed_processes() == []

    asyncio.run(scenario())


FAST = ReconnectPolicy(base_delay_s=0.01, max_delay_s=0.01)


def _drop_first_connection(first: _Handler, rest: _Handler) -> _Handler:
    count = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal count
        count += 1
        await (first if count == 1 else rest)(ws)

    return handler


async def coinbase_snapshot_then_drop(ws: ServerConnection) -> None:
    for _ in range(3):
        await ws.recv()
    await ws.send(coinbase_message("subscriptions", 0, [{"subscriptions": {}}]))
    await ws.send(coinbase_snapshot(1))
    await asyncio.sleep(0.3)
    await ws.close()


async def kraken_snapshot_then_drop(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)
    await ws.send(KRAKEN_SNAPSHOT)
    await asyncio.sleep(0.3)
    await ws.close()


async def kraken_heartbeats_without_a_book(ws: ServerConnection) -> None:
    for _ in range(2):
        await ws.recv()
    await ws.send(KRAKEN_ACK)
    try:
        while True:
            await asyncio.sleep(0.05)
            await ws.send(KRAKEN_HEARTBEAT)
    except ConnectionClosed:
        pass


def test_live_session_survives_a_dropped_feed_and_records_the_reconnect(
    two_venue_live_engine_address: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> tuple[LiveSession, SessionResult]:
        coinbase = _drop_first_connection(coinbase_snapshot_then_drop, coinbase_feed)
        async with serve(kraken_feed, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                channel = EngineChannel(two_venue_live_engine_address)
                try:
                    await channel.wait_ready(5.0)
                    session = LiveSession(
                        channel,
                        [OrderSpec("live-reconnect", "buy", 0.02, 2, 2)],
                        "BTC/USD",
                        logging.getLogger("test"),
                        venues=("kraken", "coinbase"),
                        fee_bps=await channel.venue_fees(),
                    )
                    result = await session.run(
                        two_venue_live_engine_address,
                        urls=urls,
                        book_timeout_s=10.0,
                        reconnect=FAST,
                    )
                finally:
                    await channel.close()
        return session, result

    with caplog.at_level(logging.WARNING, logger="test"):
        session, result = asyncio.run(scenario())
    assert result.statuses[0].state in (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED)
    assert [(r.venue, r.attempt, r.recovered) for r in session.reconnects] == [
        ("coinbase", 1, True)
    ]
    (record,) = [r for r in caplog.records if r.getMessage() == "feed reconnect"]
    fields = record.fields  # type: ignore[attr-defined]
    assert (fields["event"], fields["venue"], fields["attempt"]) == (
        "feed_reconnect",
        "coinbase",
        1,
    )
    assert fields["recovered"] is True
    assert our_feed_processes() == []


def test_no_usable_venue_for_the_stale_limit_ends_the_run(live_engine_address: str) -> None:
    async def scenario() -> LiveSession:
        kraken = _drop_first_connection(kraken_snapshot_then_drop, kraken_heartbeats_without_a_book)
        async with serve(kraken, "127.0.0.1", 0) as server:
            urls: dict[Venue, str] = {"kraken": f"ws://127.0.0.1:{port_of(server)}"}
            channel = EngineChannel(live_engine_address)
            try:
                await channel.wait_ready(5.0)
                session = LiveSession(
                    channel,
                    [OrderSpec("live-stale", "buy", 0.02, 30, 5)],
                    "BTC/USD",
                    logging.getLogger("test"),
                    venues=("kraken",),
                    fee_bps=await channel.venue_fees(),
                )
                with pytest.raises(LiveFeedError, match="no venue had a fresh book for 1s"):
                    await session.run(
                        live_engine_address,
                        urls=urls,
                        book_timeout_s=10.0,
                        reconnect=FAST,
                        stale_limit_s=1.0,
                        stale_poll_s=0.05,
                    )
            finally:
                await channel.close()
        return session

    session = asyncio.run(scenario())
    assert [(r.venue, r.attempt, r.recovered) for r in session.reconnects] == [("kraken", 1, False)]
    assert our_feed_processes() == []
