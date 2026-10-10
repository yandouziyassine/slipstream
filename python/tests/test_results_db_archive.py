from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from slipstream.db import ResultsDB
from slipstream.db.results_db import DayRecording, FillRow, RecordingUpload
from slipstream.models import Fill
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules

FEES = {"kraken": 40.0, "coinbase": 60.0}
RULES = {"kraken": VenueRules(min_qty=0.00005, qty_step=1e-8, min_notional=0.5)}
SHA = "a" * 64
OTHER_SHA = "b" * 64
D1 = date(2026, 9, 27)
D2 = date(2026, 9, 28)
TODAY = date(2026, 9, 29)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[ResultsDB]:
    instance = ResultsDB(tmp_path / "slipstream.db")
    instance.migrate()
    try:
        yield instance
    finally:
        instance.close()


def _at(day: date, hour: int = 10, minute: int = 5) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, 0, tzinfo=UTC)


def _run(db: ResultsDB, day: date, hour: int = 10, qty: float = 0.01, finish: bool = True) -> int:
    run_id = db.begin_run(_at(day, hour), "buy", qty, 600, FEES, RULES, "abc123")
    if finish:
        db.finish_run(run_id, "completed", _at(day, hour, 20))
    return run_id


def _record(db: ResultsDB, run_id: int, day: date, hour: int = 10) -> Path:
    path = Path(f"/data/recordings/{day.isoformat()}/{hour:02d}-0p01.jsonl.gz")
    db.set_recording(run_id, path, SHA, _at(day, hour, 16))
    return path


def _add_fills(db: ResultsDB, run_id: int, count: int = 2) -> None:
    status = pb.OrderStatus(
        order_id="o1",
        algo="twap",
        state=pb.ORDER_STATE_COMPLETED,
        total_qty=0.01,
        filled_qty=0.01,
        avg_fill_price=100000.0,
        arrival_mid=100000.0,
    )
    fills = [
        Fill(order_id="o1", ts_ns=10 + i, qty=0.005, price=100000.0 + i, venue="kraken", fee=0.02)
        for i in range(count)
    ]
    db.add_results(
        run_id, [status], fills, pb.EngineStats(events=1, latency_p50_ns=1, latency_p99_ns=2)
    )


def _archive(
    db: ResultsDB, day: date, uploads: list[RecordingUpload] | None = None, oid: str = "c" * 40
) -> None:
    db.record_archive_day(
        day,
        oid,
        f"manifests/{day:%Y/%m/%d}.jsonl",
        SHA,
        f"fills/{day:%Y/%m/%d}.csv.gz",
        OTHER_SHA,
        4,
        uploads or [],
        _at(day + timedelta(days=1), 0, 30),
    )


def _upload(run_id: int, day: date) -> RecordingUpload:
    return RecordingUpload(run_id, f"recordings/{day:%Y/%m/%d}/10-0p01.jsonl.gz", SHA, 123)


def _versions(db: ResultsDB) -> list[int]:
    rows = db.connection.execute("SELECT version FROM schema_version ORDER BY version")
    return [row[0] for row in rows]


def test_migration_applies_to_an_empty_file(db: ResultsDB) -> None:
    assert _versions(db) == [1, 2, 3]


def test_migration_applies_to_a_version_two_database(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    migrations = Path(__file__).parents[1] / "slipstream/db/migrations"
    old = sqlite3.connect(path)
    old.executescript((migrations / "001_initial.sql").read_text())
    old.executescript((migrations / "002_feed_reconnects.sql").read_text())
    old.execute(
        "INSERT INTO runs (started_at, status, side, qty, duration_s, fees_json, "
        "venue_rules_json) VALUES ('2026-09-27T10:00:00', 'completed', 'buy', 0.01, 600, '{}', "
        "'{}')"
    )
    old.commit()
    old.close()

    migrated = ResultsDB(path)
    try:
        migrated.migrate()
        assert _versions(migrated) == [1, 2, 3]
        assert migrated.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        migrated.close()


def test_migrate_twice_is_a_no_op(db: ResultsDB) -> None:
    db.migrate()

    assert _versions(db) == [1, 2, 3]


def test_archive_tables_are_append_only(db: ResultsDB) -> None:
    run_id = _run(db, D1)
    _archive(db, D1, [_upload(run_id, D1)])

    for sql in (
        "UPDATE archive_days SET commit_oid = 'x'",
        "DELETE FROM archive_days",
        "UPDATE recording_uploads SET bytes = 1",
        "DELETE FROM recording_uploads",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.connection.execute(sql)


def test_a_second_upload_row_for_the_same_run_is_refused(db: ResultsDB) -> None:
    run_id = _run(db, D1)
    _archive(db, D1, [_upload(run_id, D1)])

    with pytest.raises(sqlite3.IntegrityError):
        _archive(db, D2, [_upload(run_id, D1)])
    assert db.connection.execute("SELECT COUNT(*) FROM archive_days").fetchone()[0] == 1


def test_a_day_cannot_be_archived_twice(db: ResultsDB) -> None:
    _run(db, D1)
    _archive(db, D1)

    with pytest.raises(sqlite3.IntegrityError):
        _archive(db, D1)


def test_ready_days_exclude_today_and_days_without_runs(db: ResultsDB) -> None:
    _run(db, D1)
    _run(db, TODAY)

    assert db.archive_ready_days(TODAY) == [D1]


def test_ready_days_with_no_runs_is_empty(db: ResultsDB) -> None:
    assert db.archive_ready_days(TODAY) == []


def test_ready_days_exclude_archived_days(db: ResultsDB) -> None:
    _run(db, D1)
    _run(db, D2)
    _archive(db, D1)

    assert db.archive_ready_days(TODAY) == [D2]


def test_a_started_run_blocks_its_day_until_it_is_abandoned(db: ResultsDB) -> None:
    _run(db, D1, hour=9)
    _run(db, D1, hour=10, finish=False)

    assert db.archive_ready_days(TODAY) == []

    db.mark_abandoned(_at(D1, 13))

    assert db.archive_ready_days(TODAY) == [D1]


def test_ready_days_come_oldest_first(db: ResultsDB) -> None:
    _run(db, D2)
    _run(db, D1)

    assert db.archive_ready_days(TODAY) == [D1, D2]


def test_a_day_with_only_failed_runs_is_ready(db: ResultsDB) -> None:
    run_id = _run(db, D1, finish=False)
    db.finish_run(run_id, "failed", _at(D1, 11), error="boom")

    assert db.archive_ready_days(TODAY) == [D1]


def test_day_recordings_return_only_that_days_runs_in_run_order(db: ResultsDB) -> None:
    first = _run(db, D1, hour=9)
    second = _run(db, D1, hour=10, qty=0.25)
    other_day = _run(db, D2)
    paths = {
        first: _record(db, first, D1, 9),
        second: _record(db, second, D1, 10),
        other_day: _record(db, other_day, D2),
    }

    rows = db.day_recordings(D1)

    assert rows == [
        DayRecording(first, paths[first], SHA, _at(D1, 9), _at(D1, 9, 16), 0.01),
        DayRecording(second, paths[second], SHA, _at(D1, 10), _at(D1, 10, 16), 0.25),
    ]


def test_day_recordings_skip_deleted_recordings(db: ResultsDB) -> None:
    kept = _run(db, D1, hour=9)
    gone = _run(db, D1, hour=10)
    _record(db, kept, D1, 9)
    _record(db, gone, D1, 10)
    db.delete_recordings([gone], _at(TODAY))

    assert [row.run_id for row in db.day_recordings(D1)] == [kept]


def test_day_fills_return_only_that_days_fills_in_id_order(db: ResultsDB) -> None:
    first = _run(db, D1)
    other_day = _run(db, D2)
    second = _run(db, D1, hour=11)
    _add_fills(db, first, 2)
    _add_fills(db, other_day, 3)
    _add_fills(db, second, 1)

    rows = db.day_fills(D1)

    assert rows == [
        FillRow(1, first, "twap", "kraken", 0.005, 100000.0, 0.02, 10),
        FillRow(2, first, "twap", "kraken", 0.005, 100001.0, 0.02, 11),
        FillRow(6, second, "twap", "kraken", 0.005, 100000.0, 0.02, 10),
    ]


def test_record_archive_day_writes_both_tables(db: ResultsDB) -> None:
    run_id = _run(db, D1)
    _record(db, run_id, D1)

    _archive(db, D1, [_upload(run_id, D1)], oid="d" * 40)

    day = db.connection.execute(
        "SELECT id, day, commit_oid, manifest_path, manifest_sha256, fills_path, fills_sha256, "
        "fills_rows, uploaded_at FROM archive_days"
    ).fetchone()
    upload = db.connection.execute(
        "SELECT run_id, archive_day_id, path_in_repo, sha256, bytes FROM recording_uploads"
    ).fetchone()
    assert day == (
        1,
        "2026-09-27",
        "d" * 40,
        "manifests/2026/09/27.jsonl",
        SHA,
        "fills/2026/09/27.csv.gz",
        OTHER_SHA,
        4,
        "2026-09-28T00:30:00",
    )
    assert upload == (run_id, 1, "recordings/2026/09/27/10-0p01.jsonl.gz", SHA, 123)


def test_record_archive_day_is_atomic(db: ResultsDB) -> None:
    run_id = _run(db, D1)

    with pytest.raises(sqlite3.IntegrityError):
        _archive(db, D1, [_upload(run_id, D1), _upload(999, D1)])

    assert db.connection.execute("SELECT COUNT(*) FROM archive_days").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM recording_uploads").fetchone()[0] == 0
    assert not db.is_uploaded(run_id)


def test_is_uploaded(db: ResultsDB) -> None:
    uploaded = _run(db, D1, hour=9)
    waiting = _run(db, D1, hour=10)
    _archive(db, D1, [_upload(uploaded, D1)])

    assert db.is_uploaded(uploaded)
    assert not db.is_uploaded(waiting)
    assert not db.is_uploaded(12345)


def _three_recordings(db: ResultsDB) -> tuple[int, int, int]:
    """An old uploaded recording, an old waiting one, and a recent uploaded one."""
    old_up = _run(db, D1, hour=9)
    old_wait = _run(db, D1, hour=10)
    recent_up = _run(db, D2, hour=9)
    for run_id, day, hour in ((old_up, D1, 9), (old_wait, D1, 10), (recent_up, D2, 9)):
        _record(db, run_id, day, hour)
    _archive(db, D1, [_upload(old_up, D1)])
    _archive(db, D2, [_upload(recent_up, D2)])
    return old_up, old_wait, recent_up


def test_uploaded_recordings_older_than_returns_only_old_uploaded_ones(db: ResultsDB) -> None:
    old_up, _old_wait, recent_up = _three_recordings(db)
    now = _at(D1 + timedelta(days=8), 0, 10)

    rows = db.uploaded_recordings_older_than(now, 7)

    assert [run_id for run_id, _ in rows] == [old_up]
    assert recent_up not in [run_id for run_id, _ in rows]


def test_uploaded_recordings_older_than_skips_deleted_ones(db: ResultsDB) -> None:
    old_up, _old_wait, _recent_up = _three_recordings(db)
    db.delete_recordings([old_up], _at(TODAY))

    assert db.uploaded_recordings_older_than(_at(D1 + timedelta(days=8), 0, 10), 7) == []


def test_unarchived_recordings_older_than_counts_the_others(db: ResultsDB) -> None:
    _three_recordings(db)
    now = _at(D1 + timedelta(days=8), 0, 10)

    assert db.unarchived_recordings_older_than(now, 7) == 1
    assert db.unarchived_recordings_older_than(_at(D1 + timedelta(days=2)), 7) == 0


def test_unarchived_count_skips_deleted_recordings(db: ResultsDB) -> None:
    _old_up, old_wait, _recent_up = _three_recordings(db)
    db.delete_recordings([old_wait], _at(TODAY))

    assert db.unarchived_recordings_older_than(_at(D1 + timedelta(days=8), 0, 10), 7) == 0


def test_active_recordings_default_order_is_unchanged(db: ResultsDB) -> None:
    old_up, old_wait, recent_up = _three_recordings(db)

    assert [run_id for run_id, _ in db.active_recordings()] == [old_up, old_wait, recent_up]


def test_active_recordings_uploaded_first(db: ResultsDB) -> None:
    old_up, old_wait, recent_up = _three_recordings(db)
    newer_wait = _run(db, D2, hour=11)
    _record(db, newer_wait, D2, 11)

    rows = db.active_recordings(uploaded_first=True)

    assert [run_id for run_id, _ in rows] == [old_up, recent_up, old_wait, newer_wait]
