from __future__ import annotations

import gzip
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slipstream.db import ResultsDB
from slipstream.storage import (
    StorageConfigError,
    StoragePolicy,
    daily_backup,
    disk_usage_mb,
    enforce_recordings_budget,
    expire_recordings,
    prune_old_logs,
)

FEES = {"kraken": 40.0, "coinbase": 60.0}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[ResultsDB]:
    instance = ResultsDB(tmp_path / "slipstream.db")
    instance.migrate()
    try:
        yield instance
    finally:
        instance.close()


def _run_with_recording(
    db: ResultsDB, path: Path, recorded_at: datetime, size_bytes: int = 100
) -> int:
    run_id = db.begin_run(recorded_at, "buy", 0.01, 600, FEES, {}, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)
    db.set_recording(run_id, path, "a" * 64, recorded_at)
    return run_id


def test_storage_policy_defaults() -> None:
    policy = StoragePolicy()
    assert policy.full_retention_days == 7
    assert policy.thin_retention_days == 90
    assert policy.thin_keep_hour_utc == 12
    assert policy.recordings_max_mb == 1024
    assert policy.backups_to_keep == 7
    assert policy.logs_max_age_days == 14


def test_storage_policy_from_env_default_when_unset() -> None:
    policy = StoragePolicy.from_env({})
    assert policy.recordings_max_mb == 1024


def test_storage_policy_from_env_accepts_positive_int() -> None:
    policy = StoragePolicy.from_env({"SLIPSTREAM_RECORDINGS_MAX_MB": "2048"})
    assert policy.recordings_max_mb == 2048


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "1.5", ""])
def test_storage_policy_from_env_rejects_invalid(raw: str) -> None:
    with pytest.raises(StorageConfigError):
        StoragePolicy.from_env({"SLIPSTREAM_RECORDINGS_MAX_MB": raw})


def test_expire_recordings_deletes_all_including_noon(db: ResultsDB, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    old_noon = now.replace(hour=12) - timedelta(days=100)
    old_other = now.replace(hour=3) - timedelta(days=100)
    recent = now - timedelta(days=1)
    noon_run = _run_with_recording(db, tmp_path / "recordings" / "noon.jsonl.gz", old_noon)
    other_run = _run_with_recording(db, tmp_path / "recordings" / "other.jsonl.gz", old_other)
    recent_run = _run_with_recording(db, tmp_path / "recordings" / "recent.jsonl.gz", recent)

    expired = expire_recordings(db, now, older_than_days=90)

    expired_names = {p.name for p in expired}
    assert expired_names == {"noon.jsonl.gz", "other.jsonl.gz"}
    deleted_run_ids = {
        row[0] for row in db.connection.execute("SELECT run_id FROM recording_deletions")
    }
    assert deleted_run_ids == {noon_run, other_run}
    assert recent_run not in deleted_run_ids

    again = expire_recordings(db, now, older_than_days=90)
    assert again == []


def test_enforce_recordings_budget_is_a_noop_under_budget(db: ResultsDB, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    for i in range(5):
        p = tmp_path / "recordings" / f"r{i}.jsonl.gz"
        _run_with_recording(db, p, now - timedelta(hours=5 - i), size_bytes=100)
    policy = StoragePolicy(recordings_max_mb=1, budget_target_fraction=0.9)

    deleted, mb = enforce_recordings_budget(db, tmp_path, policy, now)

    assert deleted == []
    assert mb == 0.0


def test_enforce_recordings_budget_evicts_when_over_and_protects_this_hour(
    db: ResultsDB, tmp_path: Path
) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    oldest = tmp_path / "recordings" / "oldest.jsonl.gz"
    middle = tmp_path / "recordings" / "middle.jsonl.gz"
    this_hour = tmp_path / "recordings" / "this_hour.jsonl.gz"
    _run_with_recording(db, oldest, now - timedelta(hours=3), size_bytes=1_000_000)
    _run_with_recording(db, middle, now - timedelta(hours=2), size_bytes=1_000_000)
    _run_with_recording(db, this_hour, now, size_bytes=1_000_000)

    policy = StoragePolicy(recordings_max_mb=2, budget_target_fraction=0.9)
    deleted, mb = enforce_recordings_budget(
        db, tmp_path, policy, now, protect=frozenset({this_hour})
    )

    assert this_hour not in deleted
    assert oldest in deleted
    assert mb > 0
    # enforce_recordings_budget only decides what to delete; the caller does the unlinking.
    assert oldest.exists()

    deleted_run_ids = {
        row[0] for row in db.connection.execute("SELECT run_id FROM recording_deletions")
    }
    assert len(deleted_run_ids) == len(deleted)


def test_enforce_recordings_budget_is_rerun_safe(db: ResultsDB, tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    oldest = tmp_path / "recordings" / "oldest.jsonl.gz"
    _run_with_recording(db, oldest, now - timedelta(hours=3), size_bytes=2_000_000)
    policy = StoragePolicy(recordings_max_mb=1, budget_target_fraction=0.9)

    first_deleted, _ = enforce_recordings_budget(db, tmp_path, policy, now)
    oldest.unlink()
    second_deleted, second_mb = enforce_recordings_budget(db, tmp_path, policy, now)

    assert oldest in first_deleted
    assert second_deleted == []
    assert second_mb == 0.0


def test_daily_backup_creates_once_per_day_and_prunes(db: ResultsDB, tmp_path: Path) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, {}, None)
    db.finish_run(run_id, "completed", datetime.now(UTC))
    now = datetime(2026, 9, 27, 5, tzinfo=UTC)

    created = daily_backup(db, tmp_path, now, keep=2)
    assert created is not None
    assert created.exists()
    assert created.name == "slipstream-2026-09-27.db.gz"
    with gzip.open(created, "rb") as handle:
        raw = handle.read()
    restored = tmp_path / "restored.db"
    restored.write_bytes(raw)
    conn = sqlite3.connect(restored)
    count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    conn.close()
    assert count == 1

    again = daily_backup(db, tmp_path, now, keep=2)
    assert again is None

    for day in range(24, 27):
        stale = tmp_path / "backup" / f"slipstream-2026-09-{day}.db.gz"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")
    daily_backup(db, tmp_path, now + timedelta(days=1), keep=2)
    remaining = sorted((tmp_path / "backup").glob("slipstream-*.db.gz"))
    assert len(remaining) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug: _prune_backups uses backups[:-keep] to drop the newest `keep` backups from the "
        "delete list. When keep=0, -keep is 0 and backups[:-0] == backups[:0] == [], so nothing "
        "is deleted and every backup is kept forever instead of none. "
        "See python/slipstream/storage.py:121-124."
    ),
)
def test_daily_backup_with_keep_zero_deletes_every_backup(db: ResultsDB, tmp_path: Path) -> None:
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, {}, None)
    db.finish_run(run_id, "completed", datetime.now(UTC))
    now = datetime(2026, 9, 27, 5, tzinfo=UTC)

    for day in range(24, 27):
        stale = tmp_path / "backup" / f"slipstream-2026-09-{day}.db.gz"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

    daily_backup(db, tmp_path, now, keep=0)

    remaining = sorted((tmp_path / "backup").glob("slipstream-*.db.gz"))
    assert remaining == []


def test_prune_old_logs_deletes_only_stale_files(tmp_path: Path) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    old = logs_dir / "old.log"
    fresh = logs_dir / "fresh.log"
    old.write_text("old")
    fresh.write_text("fresh")
    import os

    old_time = (now - timedelta(days=20)).timestamp()
    os.utime(old, (old_time, old_time))

    count = prune_old_logs(tmp_path, now, max_age_days=14)

    assert count == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_old_logs_missing_dir_is_a_noop(tmp_path: Path) -> None:
    assert prune_old_logs(tmp_path, datetime.now(UTC), max_age_days=14) == 0


def test_disk_usage_mb_reports_each_area(tmp_path: Path) -> None:
    (tmp_path / "recordings" / "2026-09-27").mkdir(parents=True)
    (tmp_path / "recordings" / "2026-09-27" / "00-0p01.jsonl.gz").write_bytes(b"x" * _MB(1))
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "slipstream-2026-09-27.db.gz").write_bytes(b"x" * _MB(2))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_bytes(b"x" * _MB(1))
    db_path = tmp_path / "slipstream.db"
    db_path.write_bytes(b"x" * _MB(3))

    usage = disk_usage_mb(tmp_path, db_path)

    assert usage["recordings_mb"] == pytest.approx(1.0, abs=0.01)
    assert usage["backups_mb"] == pytest.approx(2.0, abs=0.01)
    assert usage["logs_mb"] == pytest.approx(1.0, abs=0.01)
    assert usage["db_mb"] == pytest.approx(3.0, abs=0.01)


def _MB(n: int) -> int:
    return n * 1024 * 1024
