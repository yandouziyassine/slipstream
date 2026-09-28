from __future__ import annotations

import asyncio
import gzip
import itertools
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from slipstream.calibration import CalibrationData
from slipstream.collect import (
    ALGOS,
    CollectConfig,
    _housekeeping,
    _recording_path,
    acquire_lock,
    main,
    run_hour,
    side_for,
)
from slipstream.db import ResultsDB
from slipstream.kraken_rest import Bar
from slipstream.models import Venue
from slipstream.storage import StoragePolicy
from slipstream.venue_rules import VenueRules

RULES = {
    "kraken": VenueRules(min_qty=0.00005, qty_step=1e-8, min_notional=0.5),
    "coinbase": VenueRules(min_qty=1e-8, qty_step=1e-8, min_notional=1.0),
}


def _fake_fetch_rules(venues: object, symbol: str) -> dict[Venue, VenueRules]:
    return {venue: RULES[venue] for venue in cast("list[Venue]", venues)}


def _fake_fetch_calibration(symbol: str) -> CalibrationData:
    bars_1m = tuple(
        Bar(
            time_s=i * 60,
            open=100000.0,
            high=100001.0,
            low=99999.0,
            close=100000.0 + (i % 5),
            vwap=100000.0,
            volume=1.0,
            count=1,
        )
        for i in range(61)
    )
    bars_15m = (
        Bar(
            time_s=0,
            open=100000.0,
            high=100000.0,
            low=100000.0,
            close=100000.0,
            vwap=100000.0,
            volume=10.0,
            count=5,
        ),
    )
    return CalibrationData(bars_15m, bars_1m)


KRAKEN_ACK = json.dumps({"method": "subscribe", "success": True, "result": {}})
KRAKEN_SNAPSHOT = json.dumps(
    {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": 99990.0 - 10 * i, "qty": 1.0} for i in range(3)],
                "asks": [{"price": 100010.0 + 10 * i, "qty": 1.0} for i in range(3)],
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


def _coinbase_message(channel: str, seq: int, events: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "channel": channel,
            "client_id": "",
            "timestamp": "2026-09-26T00:00:00Z",
            "sequence_num": seq,
            "events": events,
        }
    )


def _coinbase_snapshot(seq: int) -> str:
    updates = []
    for i in range(3):
        updates.append(
            {
                "side": "bid",
                "event_time": "t",
                "price_level": str(99980 - 10 * i),
                "new_quantity": "2",
            }
        )
        updates.append(
            {
                "side": "offer",
                "event_time": "t",
                "price_level": str(100020 + 10 * i),
                "new_quantity": "2",
            }
        )
    return _coinbase_message(
        "l2_data", seq, [{"type": "snapshot", "product_id": "BTC-USD", "updates": updates}]
    )


def _coinbase_heartbeat(seq: int) -> str:
    return _coinbase_message("heartbeats", seq, [{"current_time": "t", "heartbeat_counter": seq}])


async def _kraken_feed(ws: ServerConnection) -> None:
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


async def _coinbase_feed(ws: ServerConnection) -> None:
    for _ in range(3):
        await ws.recv()
    await ws.send(_coinbase_message("subscriptions", 0, [{"subscriptions": {}}]))
    await ws.send(_coinbase_snapshot(1))
    seq = 2
    try:
        while True:
            await asyncio.sleep(0.05)
            await ws.send(_coinbase_heartbeat(seq))
            seq += 1
    except ConnectionClosed:
        pass


def _coinbase_feed_first_connection_breaks(
    counter: itertools.count[int],
) -> Callable[[ServerConnection], Awaitable[None]]:
    async def handler(ws: ServerConnection) -> None:
        attempt = next(counter)
        for _ in range(3):
            await ws.recv()
        await ws.send(_coinbase_message("subscriptions", 0, [{"subscriptions": {}}]))
        await ws.send(_coinbase_snapshot(1))
        if attempt == 0:
            await asyncio.sleep(0.2)
            await ws.send("not json")
            await ws.wait_closed()
            return
        seq = 2
        try:
            while True:
                await asyncio.sleep(0.05)
                await ws.send(_coinbase_heartbeat(seq))
                seq += 1
        except ConnectionClosed:
            pass

    return handler


def _port_of(server: object) -> int:
    return int(next(iter(server.sockets)).getsockname()[1])  # type: ignore[attr-defined]


def _db(path: Path) -> ResultsDB:
    db = ResultsDB(path)
    db.migrate()
    return db


def test_side_for_both_parities() -> None:
    assert side_for(0) == "buy"
    assert side_for(2) == "buy"
    assert side_for(1) == "sell"
    assert side_for(23) == "sell"


def test_lock_prevents_a_concurrent_second_run(tmp_path: Path) -> None:
    lock = acquire_lock(tmp_path)
    assert lock is not None
    with lock:
        assert acquire_lock(tmp_path) is None
    reacquired = acquire_lock(tmp_path)
    assert reacquired is not None
    with reacquired:
        pass


def test_run_hour_completes_two_sizes_with_recordings(
    two_venue_live_engine_address: str, tmp_path: Path
) -> None:
    async def scenario() -> None:
        async with serve(_kraken_feed, "127.0.0.1", 0) as kraken_server:
            async with serve(_coinbase_feed, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{_port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{_port_of(coinbase_server)}",
                }
                db = _db(tmp_path / "slipstream.db")
                cfg = CollectConfig(sizes=(0.001, 0.002), duration_s=4, slices=2, data_dir=tmp_path)
                now = datetime(2026, 9, 27, 10, tzinfo=UTC)
                try:
                    run_ids = await run_hour(
                        two_venue_live_engine_address,
                        cfg,
                        now,
                        "deadbeef",
                        db,
                        logging.getLogger("test"),
                        urls=urls,
                        fetch_rules=_fake_fetch_rules,
                        fetch_calibration=_fake_fetch_calibration,
                    )
                finally:
                    pass

        assert len(run_ids) == 2
        for run_id in run_ids:
            row = db.connection.execute(
                "SELECT status, side FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert row == ("completed", "buy")
            result_count = db.connection.execute(
                "SELECT COUNT(*) FROM results WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            assert result_count == 4
            fill_count = db.connection.execute(
                "SELECT COUNT(*) FROM fills WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            assert fill_count > 0
            stats_count = db.connection.execute(
                "SELECT COUNT(*) FROM engine_stats WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            assert stats_count == 1
            path_str, sha256 = db.connection.execute(
                "SELECT path, sha256 FROM recordings WHERE run_id = ?", (run_id,)
            ).fetchone()
            rec_path = Path(path_str)
            assert rec_path.exists()
            with gzip.open(rec_path, "rt", encoding="utf-8") as handle:
                lines = handle.readlines()
            assert lines
            for line in lines:
                json.loads(line)
            import hashlib

            with rec_path.open("rb") as raw:
                assert hashlib.file_digest(raw, "sha256").hexdigest() == sha256
        db.close()

    asyncio.run(scenario())


def test_feed_error_fails_one_size_and_the_next_still_runs(
    two_venue_live_engine_address: str, tmp_path: Path
) -> None:
    counter = itertools.count()

    async def scenario() -> None:
        async with serve(_kraken_feed, "127.0.0.1", 0) as kraken_server:
            handler = _coinbase_feed_first_connection_breaks(counter)
            async with serve(handler, "127.0.0.1", 0) as coinbase_server:
                urls: dict[Venue, str] = {
                    "kraken": f"ws://127.0.0.1:{_port_of(kraken_server)}",
                    "coinbase": f"ws://127.0.0.1:{_port_of(coinbase_server)}",
                }
                db = _db(tmp_path / "slipstream.db")
                cfg = CollectConfig(sizes=(0.001, 0.002), duration_s=4, slices=2, data_dir=tmp_path)
                now = datetime(2026, 9, 27, 11, tzinfo=UTC)
                run_ids = await run_hour(
                    two_venue_live_engine_address,
                    cfg,
                    now,
                    None,
                    db,
                    logging.getLogger("test"),
                    urls=urls,
                    fetch_rules=_fake_fetch_rules,
                    fetch_calibration=_fake_fetch_calibration,
                )

        assert len(run_ids) == 2
        statuses = [
            db.connection.execute(
                "SELECT status, error FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            for run_id in run_ids
        ]
        assert statuses[0][0] == "failed"
        assert statuses[0][1]
        assert statuses[1][0] == "completed"
        db.close()

    asyncio.run(scenario())


def test_hourly_engine_position_limit_fits_every_order_of_the_hour() -> None:
    # Both sizes run on one engine per hour, and working orders count towards its position limit,
    # so the limit must cover every algorithm at every size or the last orders are rejected.
    script = Path(__file__).resolve().parents[2] / "scripts" / "collect_hourly.sh"
    match = re.search(r"--max-position (\S+)", script.read_text(encoding="utf-8"))
    assert match is not None
    exposure = len(ALGOS) * sum(CollectConfig().sizes)
    assert float(match.group(1)) >= exposure


def test_housekeeping_backs_up_thins_expires_and_logs_disk_usage(tmp_path: Path) -> None:
    cfg = CollectConfig(sizes=(0.01, 0.25), data_dir=tmp_path)
    policy = StoragePolicy(
        full_retention_days=7, thin_retention_days=90, backups_to_keep=2, logs_max_age_days=14
    )
    db = _db(tmp_path / "slipstream.db")
    now = datetime(2026, 9, 27, 0, tzinfo=UTC)

    old_noon_at = now.replace(hour=12) - timedelta(days=40)
    old_noon_path = _recording_path(tmp_path, old_noon_at, 0.01)
    old_noon_path.parent.mkdir(parents=True, exist_ok=True)
    old_noon_path.write_bytes(b"x" * 100)
    noon_run = db.begin_run(old_noon_at, "buy", 0.01, 600, {"kraken": 40.0}, RULES, None)
    db.set_recording(noon_run, old_noon_path, "a" * 64, old_noon_at)
    db.finish_run(noon_run, "completed", now)

    ancient_at = now - timedelta(days=100)
    ancient_path = _recording_path(tmp_path, ancient_at, 0.01)
    ancient_path.parent.mkdir(parents=True, exist_ok=True)
    ancient_path.write_bytes(b"x" * 100)
    ancient_run = db.begin_run(ancient_at, "buy", 0.01, 600, {"kraken": 40.0}, RULES, None)
    db.set_recording(ancient_run, ancient_path, "b" * 64, ancient_at)
    db.finish_run(ancient_run, "completed", now)

    log = logging.getLogger("test-housekeeping")
    _housekeeping(cfg, policy, db, now, log)

    assert (tmp_path / "backup" / "slipstream-2026-09-27.db.gz").exists()
    assert not ancient_path.exists()
    deleted_run_ids = {
        row[0] for row in db.connection.execute("SELECT run_id FROM recording_deletions")
    }
    assert ancient_run in deleted_run_ids
    db.close()


def test_main_fails_fast_on_invalid_recordings_budget_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIPSTREAM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLIPSTREAM_RECORDINGS_MAX_MB", "not-a-number")

    result = main(["run", "--engine", "127.0.0.1:1"])

    assert result == 1
    log_files = list((tmp_path / "logs").glob("collect-*.log"))
    assert log_files
    assert "invalid_storage_policy" in log_files[0].read_text(encoding="utf-8")
