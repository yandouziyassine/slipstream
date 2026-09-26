import asyncio
import json
from pathlib import Path

import pytest
from test_kraken_rest import ohlc_body
from test_live import coinbase_sub_ack
from websockets.asyncio.server import ServerConnection, serve

from slipstream import recorder
from slipstream.coinbase import CoinbaseMessageError
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
                    urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                    clock=lambda: next(clock),
                )

    assert asyncio.run(scenario()) == 3
    channels = sorted(str(s["params"]["channel"]) for s in subscriptions)  # type: ignore[index]
    assert channels == ["book", "trade"]
    records = list(read_replay(path))
    assert [json.loads(raw) for _, raw, _ in records] == [
        json.loads(BOOK),
        json.loads(TRADE),
        {"channel": "heartbeat"},
    ]
    assert [ns for ns, _, _ in records] == [1_000, 1_010, 1_020]
    assert [venue for _, _, venue in records] == ["kraken", "kraken", "kraken"]
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
                    urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                )

    with pytest.raises(KrakenMessageError):
        asyncio.run(scenario())


def test_record_then_replay_round_trip_two_venues(tmp_path: Path) -> None:
    path = tmp_path / "multi.jsonl"
    kraken_subs: list[dict[str, object]] = []
    coinbase_subs: list[dict[str, object]] = []

    async def kraken_handler(ws: ServerConnection) -> None:
        kraken_subs.append(json.loads(await ws.recv()))
        kraken_subs.append(json.loads(await ws.recv()))
        await ws.send(BOOK)
        await ws.wait_closed()

    async def coinbase_handler(ws: ServerConnection) -> None:
        for _ in range(3):
            coinbase_subs.append(json.loads(await ws.recv()))
        await ws.send(coinbase_sub_ack(0))
        await ws.send(coinbase_sub_ack(1))
        await ws.wait_closed()

    async def scenario() -> int:
        async with serve(kraken_handler, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_handler, "127.0.0.1", 0) as coinbase_server:
                urls = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                with open_new_file(path) as handle:
                    return await record_stream(
                        handle,
                        "BTC/USD",
                        10,
                        duration_s=0.5,
                        venues=("kraken", "coinbase"),
                        urls=urls,
                    )

    assert asyncio.run(scenario()) == 3
    assert len(kraken_subs) == 2
    assert [s["channel"] for s in coinbase_subs] == ["level2", "market_trades", "heartbeats"]
    records = list(read_replay(path))
    assert sorted(venue for _, _, venue in records) == ["coinbase", "coinbase", "kraken"]
    kraken_raw = [json.loads(raw) for _, raw, venue in records if venue == "kraken"]
    assert kraken_raw == [json.loads(BOOK)]


def test_hostile_coinbase_message_aborts_recording(tmp_path: Path) -> None:
    path = tmp_path / "bad_coinbase.jsonl"

    async def coinbase_handler(ws: ServerConnection) -> None:
        for _ in range(3):
            await ws.recv()
        await ws.send("[" * 100_000)
        await ws.wait_closed()

    async def scenario() -> int:
        async with serve(coinbase_handler, "127.0.0.1", 0) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=2,
                    venues=("coinbase",),
                    urls={"coinbase": f"ws://127.0.0.1:{port_of(server)}"},
                )

    with pytest.raises(CoinbaseMessageError):
        asyncio.run(scenario())


def test_default_clock_is_the_monotonic_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(5_000, 6_000, 7))
    monkeypatch.setattr(recorder, "wall_clock", lambda: lambda: next(ticks))
    path = tmp_path / "session.jsonl"

    async def scenario() -> int:
        async with serve_messages([BOOK, TRADE], []) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=0.5,
                    urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                )

    assert asyncio.run(scenario()) == 2
    assert [ns for ns, _, _ in read_replay(path)] == [5_000, 5_007]


def test_recording_window_starts_when_every_feed_has_delivered(tmp_path: Path) -> None:
    path = tmp_path / "barrier.jsonl"
    delay_s = 0.25
    duration_s = 0.1
    interval_s = 0.02

    async def kraken_handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.recv()
        for _ in range(40):
            await ws.send(BOOK)
            await asyncio.sleep(interval_s)

    async def coinbase_handler(ws: ServerConnection) -> None:
        for _ in range(3):
            await ws.recv()
        await asyncio.sleep(delay_s)
        await ws.send(coinbase_sub_ack(0))
        await ws.send(coinbase_sub_ack(1))
        await ws.wait_closed()

    async def scenario() -> int:
        async with serve(kraken_handler, "127.0.0.1", 0) as kraken_server:
            async with serve(coinbase_handler, "127.0.0.1", 0) as coinbase_server:
                urls = {
                    "kraken": f"ws://127.0.0.1:{port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{port_of(coinbase_server)}",
                }
                with open_new_file(path) as handle:
                    return await record_stream(
                        handle,
                        "BTC/USD",
                        10,
                        duration_s=duration_s,
                        venues=("kraken", "coinbase"),
                        urls=urls,
                    )

    asyncio.run(scenario())
    records = list(read_replay(path))
    coinbase_records = [r for r in records if r[2] == "coinbase"]
    kraken_records = [r for r in records if r[2] == "kraken"]
    # Coinbase's first message only arrives after `delay_s`. If the window had
    # started at connect time (the old bug) instead of at the barrier, the
    # duration_s deadline would already be gone by then and coinbase would be
    # recorded with nothing.
    assert coinbase_records
    # Kraken messages sent while waiting for coinbase to deliver its first
    # message are still recorded (pre-barrier), not just the ones inside a
    # naive duration_s window measured from connect time.
    assert len(kraken_records) >= int(delay_s / interval_s) - 1


def test_raw_message_written_verbatim_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "verbatim.jsonl"
    raw = (
        '{"channel": "trade", "type": "update", '
        '"data": [{"symbol": "BTC/USD", "price": 100.10000000, "qty": 0.50000000}]}'
    )

    async def scenario() -> int:
        async with serve_messages([raw], []) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=0.2,
                    urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                )

    assert asyncio.run(scenario()) == 1
    line = path.read_text(encoding="utf-8").strip()
    # The exact decimal text from the exchange is preserved (no re-encode
    # through float repr, which would drop the trailing zeros).
    assert "100.10000000" in line
    assert "0.50000000" in line
    records = list(read_replay(path))
    assert len(records) == 1
    recv_ns, out_raw, venue = records[0]
    assert isinstance(recv_ns, int)
    assert venue == "kraken"
    assert json.loads(out_raw) == json.loads(raw)


def test_message_with_embedded_newline_is_re_encoded(tmp_path: Path) -> None:
    path = tmp_path / "pretty.jsonl"
    pretty = '{\n  "channel": "heartbeat"\n}'

    async def scenario() -> int:
        async with serve_messages([pretty], []) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle,
                    "BTC/USD",
                    10,
                    duration_s=0.2,
                    urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
                )

    assert asyncio.run(scenario()) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    records = list(read_replay(path))
    assert len(records) == 1
    _, out_raw, _ = records[0]
    assert json.loads(out_raw) == {"channel": "heartbeat"}


def test_write_failure_surfaces_as_record_error() -> None:
    class FailingHandle:
        def write(self, data: str) -> int:
            raise OSError("disk full")

    async def scenario() -> int:
        async with serve_messages([BOOK], []) as server:
            return await record_stream(
                FailingHandle(),  # type: ignore[arg-type]
                "BTC/USD",
                10,
                duration_s=0.2,
                urls={"kraken": f"ws://127.0.0.1:{port_of(server)}"},
            )

    with pytest.raises(RecordError, match="write"):
        asyncio.run(scenario())
