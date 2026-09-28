from __future__ import annotations

import gzip
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slipstream.db import ResultsDB

_MB = 1024 * 1024
_RECORDINGS_MAX_MB_ENV = "SLIPSTREAM_RECORDINGS_MAX_MB"


class StorageConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoragePolicy:
    full_retention_days: int = 7
    thin_retention_days: int = 90
    thin_keep_hour_utc: int = 12
    recordings_max_mb: int = 1024
    budget_target_fraction: float = 0.9
    backups_to_keep: int = 7
    logs_max_age_days: int = 14
    housekeeping_nice: int = 10

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> StoragePolicy:
        env = environ if environ is not None else os.environ
        raw = env.get(_RECORDINGS_MAX_MB_ENV)
        if raw is None:
            return cls()
        try:
            value = int(raw)
        except ValueError as exc:
            raise StorageConfigError(
                f"{_RECORDINGS_MAX_MB_ENV} must be a positive integer, got {raw!r}"
            ) from exc
        if value <= 0:
            raise StorageConfigError(
                f"{_RECORDINGS_MAX_MB_ENV} must be a positive integer, got {raw!r}"
            )
        return cls(recordings_max_mb=value)


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def _db_size_bytes(db_path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def disk_usage_mb(data_dir: Path, db_path: Path) -> dict[str, float]:
    return {
        "recordings_mb": round(_dir_size_bytes(data_dir / "recordings") / _MB, 2),
        "db_mb": round(_db_size_bytes(db_path) / _MB, 2),
        "backups_mb": round(_dir_size_bytes(data_dir / "backup") / _MB, 2),
        "logs_mb": round(_dir_size_bytes(data_dir / "logs") / _MB, 2),
    }


def expire_recordings(db: ResultsDB, now: datetime, older_than_days: int) -> list[Path]:
    """Delete every recording (including any noon survivor of thinning) past the final tier."""
    expired = db.expired_recordings(now, older_than_days)
    if not expired:
        return []
    db.delete_recordings([run_id for run_id, _ in expired], now)
    return [path for _, path in expired]


def enforce_recordings_budget(
    db: ResultsDB,
    data_dir: Path,
    policy: StoragePolicy,
    now: datetime,
    protect: frozenset[Path] = frozenset(),
) -> tuple[list[Path], float]:
    """Delete the oldest recordings, never one in `protect`, until at or under the target
    fraction of the budget. Returns the paths to unlink and the total MB they occupy.
    """
    recordings_dir = data_dir / "recordings"
    budget_bytes = policy.recordings_max_mb * _MB
    total = _dir_size_bytes(recordings_dir)
    if total <= budget_bytes:
        return [], 0.0
    target_bytes = int(budget_bytes * policy.budget_target_fraction)
    run_ids: list[int] = []
    paths: list[Path] = []
    deleted_bytes = 0
    for run_id, path in db.active_recordings():
        if total <= target_bytes:
            break
        if path in protect:
            continue
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        total -= size
        deleted_bytes += size
        run_ids.append(run_id)
        paths.append(path)
    if run_ids:
        db.delete_recordings(run_ids, now)
    return paths, deleted_bytes / _MB


def _prune_backups(backup_dir: Path, keep: int) -> None:
    backups = sorted(backup_dir.glob("slipstream-*.db.gz"))
    for stale in backups[:-keep]:
        stale.unlink(missing_ok=True)


def daily_backup(db: ResultsDB, data_dir: Path, now: datetime, keep: int) -> Path | None:
    """Write today's backup (SQLite backup API, then gzip, then an atomic rename) if it does
    not already exist, then prune old backups. Safe to call every hour: only the first call of
    the day does any work.
    """
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")
    backup_dir = data_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"slipstream-{now:%Y-%m-%d}.db.gz"
    created: Path | None = None
    if not dest.exists():
        tmp_db = backup_dir / f".slipstream-{now:%Y-%m-%d}.db.tmp"
        tmp_gz = backup_dir / f".slipstream-{now:%Y-%m-%d}.db.gz.tmp"
        tmp_db.unlink(missing_ok=True)
        tmp_gz.unlink(missing_ok=True)
        try:
            db.backup(tmp_db)
            with tmp_db.open("rb") as raw, gzip.open(tmp_gz, "wb", compresslevel=9) as gz:
                shutil.copyfileobj(raw, gz)
            os.replace(tmp_gz, dest)
            created = dest
        finally:
            tmp_db.unlink(missing_ok=True)
            tmp_gz.unlink(missing_ok=True)
    _prune_backups(backup_dir, keep)
    return created


def prune_old_logs(data_dir: Path, now: datetime, max_age_days: int) -> int:
    logs_dir = data_dir / "logs"
    if not logs_dir.exists():
        return 0
    cutoff = now - timedelta(days=max_age_days)
    count = 0
    for entry in logs_dir.iterdir():
        if not entry.is_file():
            continue
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            entry.unlink(missing_ok=True)
            count += 1
    return count
