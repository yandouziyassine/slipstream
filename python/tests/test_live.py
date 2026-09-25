import asyncio
import json
import logging

import pytest
from conftest import FakeEngine
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from slipstream.live import LiveFeedError, run_live
from slipstream.models import MarketDataError, OrderSpec
from slipstream.runner import ExecutionRunner

SNAPSHOT = json.dumps(
    {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 99.0, "qty": 1.0}],
                "asks": [{"price": 101.0, "qty": 1.0}],
            }
        ],
    }
)

KRAKEN_SUBSCRIBE_ACK = json.dumps({"method": "subscribe", "success": True, "result": {}})


def coinbase_sub_ack(seq: int) -> str:
    return json.dumps(
        {
            "channel": "subscriptions",
            "client_id": "",
            "timestamp": "t",
            "sequence_num": seq,
            "events": [{"subscriptions": {}}],
        }
    )


def coinbase_snapshot(seq: int) -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "client_id": "",
            "timestamp": "2026-09-24T00:00:00Z",
            "sequence_num": seq,
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {
                            "side": "bid",
                            "event_time": "t",
                            "price_level": "99",
                            "new_quantity": "1",
                        },
                        {
                            "side": "offer",
                            "event_time": "t",
                            "price_level": "101",
                            "new_quantity": "1",
                        },
                    ],
                }
            ],
        }
    )


def make_runner(engine: FakeEngine) -> ExecutionRunner:
    return ExecutionRunner(
        engine, OrderSpec("o-1", "buy", 1.0, 4, 4), "BTC/USD", logging.getLogger("t")
    )


def make_multi_venue_runner(engine: FakeEngine) -> ExecutionRunner:
    return ExecutionRunner(
        engine,
        OrderSpec("o-1", "buy", 1.0, 4, 4),
        "BTC/USD",
        logging.getLogger("t"),
        venues=("kraken", "coinbase"),
    )


def serve_messages(messages: list[str], received: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    async def handler(ws: ServerConnection) -> None:
        received.append(json.loads(await ws.recv()))
        for message in messages:
            await ws.send(message)
        await ws.wait_closed()

    return serve(handler, "127.0.0.1", 0)


def port_of(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


def test_streams_until_order_done(fake_engine: FakeEngine) -> None:
    fake_engine.done_after_steps = 2
    received: list[dict[str, object]] = []
    messages = [
        KRAKEN_SUBSCRIBE_ACK,
        SNAPSHOT,
        json.dumps({"channel": "heartbeat"}),
    ]

    async def scenario() -> None:
        async with serve_messages(messages, received) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(
                make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, urls={"kraken": url}
            )

    asyncio.run(scenario())
    assert received[0]["params"] == {
        "channel": "book",
        "symbol": ["BTC/USD"],
        "depth": 10,
        "snapshot": True,
    }
    assert received[0]["params"]["channel"] == "book"  # type: ignore[index]
    assert len(fake_engine.steps) == 2


def test_subscribes_to_book_then_trades(fake_engine: FakeEngine) -> None:
    fake_engine.done_after_steps = 1
    received: list[dict[str, object]] = []

    async def handler(ws: ServerConnection) -> None:
        received.append(json.loads(await ws.recv()))
        received.append(json.loads(await ws.recv()))
        await ws.send(SNAPSHOT)
        await ws.wait_closed()

    async def scenario() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(
                make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, urls={"kraken": url}
            )

    asyncio.run(scenario())
    assert [r["params"]["channel"] for r in received] == ["book", "trade"]  # type: ignore[index]


def test_deadline_raises_when_no_data(fake_engine: FakeEngine) -> None:
    received: list[dict[str, object]] = []

    async def scenario() -> None:
        async with serve_messages([], received) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(
                make_runner(fake_engine), "BTC/USD", 10, deadline_s=0.5, urls={"kraken": url}
            )

    with pytest.raises(LiveFeedError):
        asyncio.run(scenario())


def test_connection_failure_raises_live_feed_error(fake_engine: FakeEngine) -> None:
    with pytest.raises(LiveFeedError):
        asyncio.run(
            run_live(
                make_runner(fake_engine),
                "BTC/USD",
                10,
                deadline_s=2,
                urls={"kraken": "ws://127.0.0.1:1"},
            )
        )


def test_streams_two_venues_concurrently(fake_engine: FakeEngine) -> None:
    fake_engine.done_after_steps = 1
    kraken_received: list[dict[str, object]] = []
    coinbase_received: list[dict[str, object]] = []

    async def kraken_handler(ws: ServerConnection) -> None:
        kraken_received.append(json.loads(await ws.recv()))
        kraken_received.append(json.loads(await ws.recv()))
        await ws.send(KRAKEN_SUBSCRIBE_ACK)
        await ws.send(SNAPSHOT)
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                await ws.send(json.dumps({"channel": "heartbeat"}))
        except ConnectionClosed:
            pass

    async def coinbase_handler(ws: ServerConnection) -> None:
        for _ in range(3):
            coinbase_received.append(json.loads(await ws.recv()))
        await ws.send(coinbase_sub_ack(0))
        await ws.send(coinbase_sub_ack(1))
        await ws.send(coinbase_sub_ack(2))
        await ws.send(coinbase_snapshot(3))
        await ws.wait_closed()

    async def scenario() -> None:
        async with serve(kraken_handler, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_handler, "127.0.0.1", 0) as coinbase_server:
                urls = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                await run_live(
                    make_multi_venue_runner(fake_engine),
                    "BTC/USD",
                    10,
                    deadline_s=5,
                    venues=("kraken", "coinbase"),
                    urls=urls,
                )

    asyncio.run(scenario())
    assert [r["channel"] for r in coinbase_received] == ["level2", "market_trades", "heartbeats"]
    assert len(fake_engine.submits) == 1


def test_symbol_mismatch_from_runner_propagates_as_market_data_error(
    fake_engine: FakeEngine,
) -> None:
    mismatched_snapshot = json.dumps(
        {
            "channel": "book",
            "type": "snapshot",
            "data": [
                {
                    "symbol": "ETH/USD",
                    "bids": [{"price": 99.0, "qty": 1.0}],
                    "asks": [{"price": 101.0, "qty": 1.0}],
                }
            ],
        }
    )
    received: list[dict[str, object]] = []

    async def scenario() -> None:
        async with serve_messages([KRAKEN_SUBSCRIBE_ACK, mismatched_snapshot], received) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(
                make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, urls={"kraken": url}
            )

    with pytest.raises(MarketDataError) as excinfo:
        asyncio.run(scenario())
    assert not isinstance(excinfo.value, LiveFeedError)


def test_one_feed_failing_cancels_the_other_before_idle_timeout(
    fake_engine: FakeEngine,
) -> None:
    async def kraken_handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.recv()
        # Never sends anything: this feed's recv() blocks until cancelled.
        await ws.wait_closed()

    async def coinbase_handler(ws: ServerConnection) -> None:
        for _ in range(3):
            await ws.recv()
        # Closes immediately: the client's next recv() raises ConnectionClosed.

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with serve(kraken_handler, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_handler, "127.0.0.1", 0) as coinbase_server:
                urls = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                with pytest.raises(LiveFeedError) as excinfo:
                    await run_live(
                        make_multi_venue_runner(fake_engine),
                        "BTC/USD",
                        10,
                        deadline_s=5,
                        idle_timeout_s=20,
                        venues=("kraken", "coinbase"),
                        urls=urls,
                    )
                assert not isinstance(excinfo.value, ExceptionGroup)
        return loop.time() - start

    elapsed = asyncio.run(scenario())
    # Proves the TaskGroup cancelled the kraken feed's blocked recv() instead of
    # waiting out idle_timeout_s (20s) or deadline_s (5s).
    assert elapsed < 2.0
