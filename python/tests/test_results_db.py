from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slipstream.db import ResultsDB, ResultsDBError
from slipstream.models import Fill
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules
from slipstream.venue_ws import FeedReconnect

FEES = {"kraken": 40.0, "coinbase": 60.0}
RULES = {
    "kraken": VenueRules(min_qty=0.00005, qty_step=1e-8, min_notional=0.5),
    "coinbase": VenueRules(min_qty=1e-8, qty_step=1e-8, min_notional=1.0),
}


def _db(path: Path) -> ResultsDB:
    db = ResultsDB(path)
    db.migrate()
    return db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[ResultsDB]:
    instance = _db(tmp_path / "slipstream.db")
    try:
        yield instance
    finally:
        instance.close()


def _status(
    order_id: str = "h2026092700-0.01-twap",
    algo: str = "twap",
    state: int = pb.ORDER_STATE_COMPLETED,
    halt_reason: str = "",
) -> pb.OrderStatus:
    return pb.OrderStatus(
        order_id=order_id,
        algo=algo,
        state=state,
        total_qty=0.01,
        filled_qty=0.01,
        avg_fill_price=100000.0,
        arrival_mid=100000.0,
        slippage_bps=1.5,
        immediate_cost_bps=2.0,
        fees_bps=0.4,
        routed_all_in_bps=1.9,
        halt_reason=halt_reason,
        venue_costs=[
            pb.VenueCost(venue="kraken", all_in_bps=2.1, available=True),
            pb.VenueCost(venue="coinbase", all_in_bps=2.5, available=True),
        ],
    )


def _fill(order_id: str, venue: str = "kraken") -> Fill:
    return Fill(order_id=order_id, ts_ns=1, qty=0.005, price=100000.0, venue=venue, fee=0.02)


def _stats() -> pb.EngineStats:
    return pb.EngineStats(events=10, latency_p50_ns=1000, latency_p99_ns=5000)


def test_migrate_creates_schema_from_empty_file(tmp_path: Path) -> None:
    db = ResultsDB(tmp_path / "fresh.db")
    try:
        db.migrate()
        tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        db.close()
    assert {"runs", "recordings", "recording_deletions", "results", "fills", "engine_stats"} <= (
        tables
    )


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    first = ResultsDB(path)
    first.migrate()
    first.close()
    second = ResultsDB(path)
    second.migrate()
    version_count = second.connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    second.close()
    assert version_count == 3


@pytest.mark.parametrize("table", ["results", "fills", "engine_stats", "recordings"])
def test_update_blocked_on_append_only_tables(db: ResultsDB, table: str) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    if table == "recordings":
        db.set_recording(run_id, Path("/data/rec.jsonl.gz"), "a" * 64, datetime.now(UTC))
        column, value = "sha256", "'b' * 64"
    elif table == "engine_stats":
        db.add_results(run_id, [], [], _stats())
        column, value = "events", "999"
    else:
        db.add_results(run_id, [_status()], [_fill("h2026092700-0.01-twap")], _stats())
        column = "algo" if table == "fills" else "state"
        value = "'tampered'"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.connection.execute(f"UPDATE {table} SET {column} = {value} WHERE id = 1")  # noqa: S608


@pytest.mark.parametrize(
    "table", ["results", "fills", "engine_stats", "recordings", "recording_deletions", "runs"]
)
def test_delete_blocked_on_every_table(db: ResultsDB, table: str) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    if table in ("recordings", "recording_deletions"):
        db.set_recording(
            run_id, Path("/data/rec.jsonl.gz"), "a" * 64, datetime.now(UTC) - timedelta(days=40)
        )
        db.thin_recordings(datetime.now(UTC), keep_hour_utc=23)
    elif table in ("results", "fills", "engine_stats"):
        db.add_results(run_id, [_status()], [_fill("h2026092700-0.01-twap")], _stats())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.connection.execute(f"DELETE FROM {table} WHERE id = 1")  # noqa: S608


def test_run_can_finish_exactly_once(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    db.finish_run(run_id, "completed", datetime.now(UTC))
    row = db.connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row[0] == "completed"
    with pytest.raises(ResultsDBError, match="not in the started state"):
        db.finish_run(run_id, "failed", datetime.now(UTC), error="boom")


def test_changing_side_during_finish_is_refused(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.connection.execute(
            "UPDATE runs SET status = 'completed', side = 'sell' WHERE id = ?", (run_id,)
        )


def test_failed_run_keeps_its_error(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "sell", 0.25, 600, FEES, RULES, None)
    db.finish_run(run_id, "failed", datetime.now(UTC), error="kraken feed dropped")
    row = db.connection.execute("SELECT status, error FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row == ("failed", "kraken feed dropped")


def test_mark_abandoned_only_touches_old_started_runs(db: ResultsDB) -> None:
    now = datetime.now(UTC)
    old_started = db.begin_run(now - timedelta(hours=3), "buy", 0.01, 600, FEES, RULES, None)
    recent_started = db.begin_run(now - timedelta(minutes=5), "buy", 0.01, 600, FEES, RULES, None)
    old_completed = db.begin_run(now - timedelta(hours=3), "buy", 0.01, 600, FEES, RULES, None)
    db.finish_run(old_completed, "completed", now - timedelta(hours=2, minutes=50))

    count = db.mark_abandoned(now)

    assert count == 1
    statuses = {
        run_id: row[0]
        for run_id in (old_started, recent_started, old_completed)
        for row in [
            db.connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        ]
    }
    assert statuses[old_started] == "abandoned"
    assert statuses[recent_started] == "started"
    assert statuses[old_completed] == "completed"


def test_thin_recordings_keeps_the_noon_recording(db: ResultsDB) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    kept_run = db.begin_run(now - timedelta(days=40), "buy", 0.01, 600, FEES, RULES, None)
    dropped_run = db.begin_run(now - timedelta(days=40), "sell", 0.01, 600, FEES, RULES, None)
    recent_run = db.begin_run(now - timedelta(days=1), "buy", 0.01, 600, FEES, RULES, None)
    db.set_recording(
        kept_run, Path("/data/kept.jsonl.gz"), "a" * 64, now.replace(hour=12) - timedelta(days=40)
    )
    db.set_recording(
        dropped_run,
        Path("/data/dropped.jsonl.gz"),
        "b" * 64,
        now.replace(hour=3) - timedelta(days=40),
    )
    db.set_recording(recent_run, Path("/data/recent.jsonl.gz"), "c" * 64, now - timedelta(days=1))

    to_delete = db.thin_recordings(now)

    assert to_delete == [Path("/data/dropped.jsonl.gz")]
    deletions = db.connection.execute("SELECT run_id FROM recording_deletions").fetchall()
    assert deletions == [(dropped_run,)]

    again = db.thin_recordings(now)
    assert again == []


def test_backup_has_the_same_row_counts(db: ResultsDB, tmp_path: Path) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    db.add_results(run_id, [_status()], [_fill("h2026092700-0.01-twap")], _stats())
    db.finish_run(run_id, "completed", datetime.now(UTC))

    dest = tmp_path / "backup.db"
    db.backup(dest)

    backup_conn = sqlite3.connect(dest)
    try:
        for table in ("runs", "results", "fills", "engine_stats"):
            original = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            copied = backup_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            assert original == copied
    finally:
        backup_conn.close()


def test_hostile_string_round_trips_as_data(db: ResultsDB) -> None:
    hostile = "'; DROP TABLE runs; --\" OR 1=1 -- \u0000"
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, hostile)
    db.finish_run(run_id, "failed", datetime.now(UTC), error=hostile)

    row = db.connection.execute(
        "SELECT git_commit, error FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row == (hostile, hostile)
    tables = db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert ("runs",) in tables


def test_add_results_resolves_algo_from_order_id_for_fills(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, None)
    statuses = [_status(order_id="h2026092700-0.01-twap", algo="twap")]
    fills = [_fill("h2026092700-0.01-twap", venue="coinbase")]

    db.add_results(run_id, statuses, fills, _stats())

    row = db.connection.execute(
        "SELECT algo, venue FROM fills WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row == ("twap", "coinbase")
    result_row = db.connection.execute(
        "SELECT fills_count, routing_gain_bps FROM results WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert result_row[0] == 1
    assert result_row[1] == pytest.approx(2.1 - 1.9)


def test_add_results_filled_pct_is_zero_when_nothing_was_ordered(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.0, 600, FEES, RULES, None)
    status = pb.OrderStatus(
        order_id="h2026092700-0-twap",
        algo="twap",
        state=pb.ORDER_STATE_HALTED,
        total_qty=0.0,
        filled_qty=0.0,
        halt_reason="risk limit exceeded before any child order was sent",
    )

    db.add_results(run_id, [status], [], _stats())

    filled_pct = db.connection.execute(
        "SELECT filled_pct FROM results WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    assert filled_pct == 0.0


def test_add_results_rejects_a_fill_for_an_unknown_order(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, None)
    with pytest.raises(ResultsDBError, match="unknown order id"):
        db.add_results(run_id, [], [_fill("no-such-order")], _stats())


def test_begin_run_rejects_invalid_side(db: ResultsDB) -> None:
    with pytest.raises(ResultsDBError, match="invalid side"):
        db.begin_run(datetime.now(UTC), "hold", 0.01, 600, FEES, RULES, None)  # type: ignore[arg-type]


def test_finish_run_rejects_non_terminal_status(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, None)
    with pytest.raises(ResultsDBError, match="completed or failed"):
        db.finish_run(run_id, "abandoned", datetime.now(UTC))  # type: ignore[arg-type]


def _reconnects() -> list[FeedReconnect]:
    return [
        FeedReconnect("coinbase", 1, "ConnectionClosedError: no close frame", 1.25, True),
        FeedReconnect("coinbase", 2, "TimeoutError: timed out", 0.5, False),
    ]


def test_reconnects_are_stored_per_run_and_source(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    db.add_reconnects(run_id, "feed", _reconnects())
    db.add_reconnects(run_id, "recorder", _reconnects()[:1])
    rows = db.connection.execute(
        "SELECT source, venue, attempt, reason, downtime_s, recovered FROM feed_reconnects "
        "WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    assert rows == [
        ("feed", "coinbase", 1, "ConnectionClosedError: no close frame", 1.25, 1),
        ("feed", "coinbase", 2, "TimeoutError: timed out", 0.5, 0),
        ("recorder", "coinbase", 1, "ConnectionClosedError: no close frame", 1.25, 1),
    ]


def test_reconnect_rows_are_append_only(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    db.add_reconnects(run_id, "feed", _reconnects())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.connection.execute("UPDATE feed_reconnects SET attempt = 9 WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.connection.execute("DELETE FROM feed_reconnects WHERE id = 1")


def test_reconnect_source_is_checked(db: ResultsDB) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, "abc123")
    with pytest.raises(ResultsDBError, match="source"):
        db.add_reconnects(run_id, "other", _reconnects())  # type: ignore[arg-type]


def test_migrating_a_version_one_database_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript(
        (Path(__file__).parents[1] / "slipstream/db/migrations/001_initial.sql").read_text()
    )
    old.execute(
        "INSERT INTO runs (started_at, status, side, qty, duration_s, fees_json, "
        "venue_rules_json) VALUES ('2026-09-27T10:00:00', 'completed', 'buy', 0.01, 600, '{}', "
        "'{}')"
    )
    old.commit()
    old.close()
    db = _db(path)
    try:
        versions = [row[0] for row in db.connection.execute("SELECT version FROM schema_version")]
        runs = db.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        db.add_reconnects(1, "feed", _reconnects())
    finally:
        db.close()
    assert sorted(versions) == [1, 2, 3]
    assert runs == 1
