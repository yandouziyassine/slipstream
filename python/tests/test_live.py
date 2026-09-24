import asyncio
import json
import logging

import pytest
from conftest import FakeEngine
from websockets.asyncio.server import ServerConnection, serve

from slipstream.live import LiveFeedError, run_live
from slipstream.models import OrderSpec
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


def make_runner(engine: FakeEngine) -> ExecutionRunner:
    return ExecutionRunner(
        engine, OrderSpec("o-1", "buy", 1.0, 4, 4), "BTC/USD", logging.getLogger("t")
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
        json.dumps({"method": "subscribe", "success": True, "result": {}}),
        SNAPSHOT,
        json.dumps({"channel": "heartbeat"}),
    ]

    async def scenario() -> None:
        async with serve_messages(messages, received) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, url=url)

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
            await run_live(make_runner(fake_engine), "BTC/USD", 10, deadline_s=5, url=url)

    asyncio.run(scenario())
    assert [r["params"]["channel"] for r in received] == ["book", "trade"]  # type: ignore[index]


def test_deadline_raises_when_no_data(fake_engine: FakeEngine) -> None:
    received: list[dict[str, object]] = []

    async def scenario() -> None:
        async with serve_messages([], received) as server:
            url = f"ws://127.0.0.1:{port_of(server)}"
            await run_live(make_runner(fake_engine), "BTC/USD", 10, deadline_s=0.5, url=url)

    with pytest.raises(LiveFeedError):
        asyncio.run(scenario())


def test_connection_failure_raises_live_feed_error(fake_engine: FakeEngine) -> None:
    with pytest.raises(LiveFeedError):
        asyncio.run(
            run_live(make_runner(fake_engine), "BTC/USD", 10, deadline_s=2, url="ws://127.0.0.1:1")
        )
