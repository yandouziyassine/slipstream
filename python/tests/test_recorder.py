import asyncio
import json
from pathlib import Path

import pytest
from test_kraken_rest import ohlc_body
from websockets.asyncio.server import ServerConnection, serve

from slipstream.kraken import KrakenMessageError
from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header
from slipstream.replay import read_calibration, read_replay

BOOK = json.dumps(
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
TRADE = json.dumps(
    {
        "channel": "trade",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "price": 100.0, "qty": 0.5}],
    }
)


def serve_messages(messages: list[str], subscriptions: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    async def handler(ws: ServerConnection) -> None:
        subscriptions.append(json.loads(await ws.recv()))
        subscriptions.append(json.loads(await ws.recv()))
        for message in messages:
            await ws.send(message)
        await ws.wait_closed()

    return serve(handler, "127.0.0.1", 0)


def port_of(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


def test_open_new_file_refuses_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "s.jsonl"
    existing.write_text("", encoding="utf-8")
    with pytest.raises(RecordError, match="exists"):
        open_new_file(existing)


def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    subscriptions: list[dict[str, object]] = []
    clock = iter(range(1_000, 10_000, 10))

    async def scenario() -> int:
        async with serve_messages(
            [BOOK, TRADE, json.dumps({"channel": "heartbeat"})], subscriptions
        ) as server:
            with open_new_file(path) as handle:
                write_ohlc_header(handle, 15, ohlc_body())
                write_ohlc_header(handle, 1, ohlc_body())
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=0.5,
                    url=f"ws://127.0.0.1:{port_of(server)}",
                    clock=lambda: next(clock),
                )

    assert asyncio.run(scenario()) == 3
    channels = sorted(str(s["params"]["channel"]) for s in subscriptions)  # type: ignore[index]
    assert channels == ["book", "trade"]
    records = list(read_replay(path))
    assert [json.loads(raw) for _, raw in records] == [
        json.loads(BOOK),
        json.loads(TRADE),
        {"channel": "heartbeat"},
    ]
    assert [ns for ns, _ in records] == [1_000, 1_010, 1_020]
    calibration = read_calibration(path)
    assert sorted(calibration) == [1, 15]
    assert len(calibration[15]) == 2


def test_hostile_stream_message_aborts_recording(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"

    async def scenario() -> int:
        async with serve_messages(["[" * 100_000], []) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=2,
                    url=f"ws://127.0.0.1:{port_of(server)}",
                )

    with pytest.raises(KrakenMessageError):
        asyncio.run(scenario())
