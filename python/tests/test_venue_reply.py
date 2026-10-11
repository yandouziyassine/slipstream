"""A venue that requires answers (e.g. Crypto.com's respond-heartbeat) gets them sent back on the
same connection by the live feed and the recorder, through the adapter's Reply results."""

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from test_feed_reconnect import _Sink, _Sleeps
from websockets.asyncio.server import ServerConnection, serve

from slipstream.feed_process import market_event, stream_venue
from slipstream.models import BookUpdate, MarketDataError
from slipstream.recorder import open_new_file, record_stream
from slipstream.venue_rules import VenueRules
from slipstream.venue_ws import ReconnectPolicy, ReconnectTracker
from slipstream.venues import registry
from slipstream.venues.base import Parsed, Parser, Reply

PING = json.dumps({"ping": 7})
PONG = json.dumps({"pong": 7})
BOOK = json.dumps({"book": [[99.0, 1.0], [101.0, 1.0]]})


def _pinger_parser(symbol: str, depth: int) -> Parser:
    def parse(raw: str | bytes) -> Parsed:
        msg = json.loads(raw)
        if "ping" in msg:
            return Reply(json.dumps({"pong": msg["ping"]}))
        if "book" in msg:
            (bid, ask) = msg["book"]
            return BookUpdate(symbol, True, (tuple(bid),), (tuple(ask),), "pinger")
        raise MarketDataError("unexpected message")

    return parse


def _pinger_rules(symbol: str, fetch: object) -> VenueRules:
    return VenueRules(0.0, 0.0, 0.0)


PINGER = dataclasses.replace(
    registry.ADAPTERS[0],
    name="pinger",
    ws_url="wss://pinger.invalid",
    subscriptions=lambda symbol, depth: [json.dumps({"subscribe": symbol})],
    new_parser=_pinger_parser,
    fetch_rules=_pinger_rules,
    new_replay_parser=None,
    new_recorded_check=None,
    resync_errors=(),
)


@pytest.fixture(autouse=True)
def _register_pinger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry._BY_NAME, "pinger", PINGER)


def _port(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


def _ping_then_book(received: list[str]):  # type: ignore[no-untyped-def]
    async def handler(ws: ServerConnection) -> None:
        received.append(str(await ws.recv()))
        await ws.send(PING)
        received.append(str(await ws.recv()))
        await ws.send(BOOK)
        await ws.wait_closed()

    return handler


def test_market_event_treats_a_reply_as_a_heartbeat() -> None:
    event = market_event(Reply(PONG), "pinger", "BTC/USD")
    assert event is not None and event.WhichOneof("event") == "heartbeat"
    assert event.heartbeat.venue == "pinger"


def test_live_feed_sends_the_reply_back_before_reading_on() -> None:
    received: list[str] = []
    sink = _Sink()

    async def scenario() -> None:
        tracker = ReconnectTracker("pinger", ReconnectPolicy(), lambda _event: None)
        async with serve(_ping_then_book(received), "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{_port(server)}"
            task = asyncio.ensure_future(
                stream_venue("pinger", "BTC/USD", 10, url, sink, tracker, 30.0, _Sleeps())
            )
            for _ in range(500):
                if "book" in sink.books() or task.done():
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert received == [json.dumps({"subscribe": "BTC/USD"}), PONG]
    assert sink.books() == ["book"]


def test_recorder_sends_the_reply_back_and_records_the_request(tmp_path: Path) -> None:
    received: list[str] = []
    path = tmp_path / "pinger.jsonl"

    async def scenario() -> int:
        async with serve(_ping_then_book(received), "127.0.0.1", 0) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=0.2,
                    venues=("pinger",),
                    urls={"pinger": f"ws://127.0.0.1:{_port(server)}"},
                )

    assert asyncio.run(scenario()) == 2
    assert received == [json.dumps({"subscribe": "BTC/USD"}), PONG]
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["msg"] for line in lines] == [json.loads(PING), json.loads(BOOK)]
