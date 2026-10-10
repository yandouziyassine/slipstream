from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Literal

from slipstream.models import Fill, Side, Venue
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules
from slipstream.venue_ws import FeedReconnect

RunStatus = Literal["completed", "failed"]
ReconnectSource = Literal["feed", "recorder"]


class ResultsDBError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class RecordingUpload:
    run_id: int
    path_in_repo: str
    sha256: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class DayRecording:
    run_id: int
    path: Path
    sha256: str
    started_at: datetime
    recorded_at: datetime
    qty: float


@dataclasses.dataclass(frozen=True)
class FillRow:
    fill_id: int
    run_id: int
    algo: str
    venue: str
    qty: float
    price: float
    fee: float
    ts_ns: int


def _iso(moment: datetime) -> str:
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def _migration_files() -> list[tuple[int, str]]:
    migrations_dir = resources.files("slipstream.db").joinpath("migrations")
    names = sorted(entry.name for entry in migrations_dir.iterdir() if entry.name.endswith(".sql"))
    files: list[tuple[int, str]] = []
    for name in names:
        version = int(name.split("_", 1)[0])
        files.append((version, migrations_dir.joinpath(name).read_text(encoding="utf-8")))
    return files


def _filled_pct(status: pb.OrderStatus) -> float:
    if status.total_qty <= 0:
        return 0.0
    return status.filled_qty / status.total_qty * 100.0


def _state_name(status: pb.OrderStatus) -> str:
    return pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")


def _routing_gain_bps(status: pb.OrderStatus) -> float | None:
    available = [cost.all_in_bps for cost in status.venue_costs if cost.available]
    if not available:
        return None
    return min(available) - status.routed_all_in_bps


class ResultsDB:
    """The append-only results database (see the collector-and-leaderboard spec, section 4).

    Every write goes through parameterised SQL; no value is ever formatted into a query string.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for read-only queries (e.g. the research site builder)."""
        return self._conn

    def __enter__(self) -> ResultsDB:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        applied = self._applied_versions()
        for version, sql in _migration_files():
            if version in applied:
                continue
            self._conn.executescript(sql)

    def _applied_versions(self) -> set[int]:
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if exists is None:
            return set()
        return {row[0] for row in self._conn.execute("SELECT version FROM schema_version")}

    def begin_run(
        self,
        started_at: datetime,
        side: Side,
        qty: float,
        duration_s: int,
        fees: Mapping[Venue, float],
        venue_rules: Mapping[Venue, VenueRules],
        git_commit: str | None,
        supersedes_id: int | None = None,
    ) -> int:
        if side not in ("buy", "sell"):
            raise ResultsDBError(f"invalid side {side!r}")
        fees_json = json.dumps(dict(fees), sort_keys=True)
        venue_rules_json = json.dumps(
            {venue: dataclasses.asdict(rules) for venue, rules in venue_rules.items()},
            sort_keys=True,
        )
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs "
                "(started_at, status, side, qty, duration_s, fees_json, venue_rules_json, "
                "git_commit, supersedes_id) "
                "VALUES (?, 'started', ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(started_at),
                    side,
                    qty,
                    duration_s,
                    fees_json,
                    venue_rules_json,
                    git_commit,
                    supersedes_id,
                ),
            )
        run_id = cursor.lastrowid
        if run_id is None:
            raise ResultsDBError("insert into runs did not return a row id")
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        ended_at: datetime,
        error: str | None = None,
    ) -> None:
        if status not in ("completed", "failed"):
            raise ResultsDBError(f"finish_run status must be completed or failed, got {status!r}")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET status = ?, ended_at = ?, error = ? "
                "WHERE id = ? AND status = 'started'",
                (status, _iso(ended_at), error, run_id),
            )
        if cursor.rowcount == 0:
            raise ResultsDBError(f"run {run_id} is not in the started state")

    def add_results(
        self,
        run_id: int,
        statuses: Sequence[pb.OrderStatus],
        fills: Sequence[Fill],
        stats: pb.EngineStats,
    ) -> None:
        algo_by_order_id = {status.order_id: status.algo for status in statuses}
        with self._conn:
            for status in statuses:
                fills_count = sum(1 for fill in fills if fill.order_id == status.order_id)
                venue_costs_json = json.dumps(
                    [
                        {
                            "venue": cost.venue,
                            "all_in_bps": cost.all_in_bps,
                            "available": cost.available,
                        }
                        for cost in status.venue_costs
                    ]
                )
                self._conn.execute(
                    "INSERT INTO results "
                    "(run_id, algo, state, filled_qty, filled_pct, avg_price, arrival_mid, "
                    "slippage_bps, fee_bps, all_in_bps, immediate_cost_bps, routing_gain_bps, "
                    "venue_costs_json, fills_count, halt_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        status.algo,
                        _state_name(status),
                        status.filled_qty,
                        _filled_pct(status),
                        status.avg_fill_price,
                        status.arrival_mid,
                        status.slippage_bps,
                        status.fees_bps,
                        status.routed_all_in_bps,
                        status.immediate_cost_bps,
                        _routing_gain_bps(status),
                        venue_costs_json,
                        fills_count,
                        status.halt_reason or None,
                    ),
                )
            for fill in fills:
                algo = algo_by_order_id.get(fill.order_id)
                if algo is None:
                    raise ResultsDBError(f"fill for unknown order id {fill.order_id!r}")
                self._conn.execute(
                    "INSERT INTO fills (run_id, algo, venue, qty, price, fee, ts_ns) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, algo, fill.venue, fill.qty, fill.price, fill.fee, fill.ts_ns),
                )
            self._conn.execute(
                "INSERT INTO engine_stats (run_id, latency_p50_ns, latency_p99_ns, events) "
                "VALUES (?, ?, ?, ?)",
                (run_id, stats.latency_p50_ns, stats.latency_p99_ns, stats.events),
            )

    def add_reconnects(
        self, run_id: int, source: ReconnectSource, reconnects: Sequence[FeedReconnect]
    ) -> None:
        if source not in ("feed", "recorder"):
            raise ResultsDBError(f"invalid reconnect source {source!r}")
        with self._conn:
            self._conn.executemany(
                "INSERT INTO feed_reconnects "
                "(run_id, source, venue, attempt, reason, downtime_s, recovered) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        source,
                        event.venue,
                        event.attempt,
                        event.reason,
                        event.downtime_s,
                        int(event.recovered),
                    )
                    for event in reconnects
                ],
            )

    def set_recording(self, run_id: int, path: Path, sha256: str, recorded_at: datetime) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO recordings (run_id, path, sha256, recorded_at) VALUES (?, ?, ?, ?)",
                (run_id, str(path), sha256, _iso(recorded_at)),
            )

    def mark_abandoned(self, now: datetime, older_than_h: float = 2.0) -> int:
        cutoff = _iso(now - timedelta(hours=older_than_h))
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE runs SET status = 'abandoned', ended_at = ? "
                "WHERE status = 'started' AND started_at <= ?",
                (_iso(now), cutoff),
            )
        return cursor.rowcount

    def thin_recordings(
        self, now: datetime, keep_hour_utc: int = 12, older_than_days: int = 30
    ) -> list[Path]:
        cutoff = _iso(now - timedelta(days=older_than_days))
        rows = self._conn.execute(
            "SELECT run_id, path, recorded_at FROM recordings "
            "WHERE recorded_at < ? AND run_id NOT IN (SELECT run_id FROM recording_deletions)",
            (cutoff,),
        ).fetchall()
        to_delete = [
            (run_id, path)
            for run_id, path, recorded_at in rows
            if _parse_iso(recorded_at).hour != keep_hour_utc
        ]
        self.delete_recordings([run_id for run_id, _ in to_delete], now)
        return [Path(path) for _, path in to_delete]

    def expired_recordings(self, now: datetime, older_than_days: int) -> list[tuple[int, Path]]:
        """Every recording older than the cutoff that has not yet been marked deleted, oldest
        first, regardless of hour (unlike `thin_recordings`, nothing is kept)."""
        cutoff = _iso(now - timedelta(days=older_than_days))
        rows = self._conn.execute(
            "SELECT run_id, path FROM recordings "
            "WHERE recorded_at < ? AND run_id NOT IN (SELECT run_id FROM recording_deletions) "
            "ORDER BY recorded_at ASC",
            (cutoff,),
        ).fetchall()
        return [(run_id, Path(path)) for run_id, path in rows]

    def active_recordings(self, uploaded_first: bool = False) -> list[tuple[int, Path]]:
        """Every recording not yet marked deleted, oldest first. With `uploaded_first`, the
        recordings that have a verified off-site copy come first (oldest first), then the rest."""
        sql = (
            "SELECT run_id, path FROM recordings "
            "WHERE run_id NOT IN (SELECT run_id FROM recording_deletions) "
        )
        if uploaded_first:
            sql += "ORDER BY (run_id NOT IN (SELECT run_id FROM recording_uploads)), recorded_at"
        else:
            sql += "ORDER BY recorded_at ASC"
        rows = self._conn.execute(sql).fetchall()
        return [(run_id, Path(path)) for run_id, path in rows]

    def is_uploaded(self, run_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM recording_uploads WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def uploaded_recordings_older_than(self, now: datetime, days: int) -> list[tuple[int, Path]]:
        """Recordings with a verified off-site copy, not yet marked deleted, older than the
        cutoff, oldest first."""
        cutoff = _iso(now - timedelta(days=days))
        rows = self._conn.execute(
            "SELECT run_id, path FROM recordings "
            "WHERE recorded_at < ? AND run_id NOT IN (SELECT run_id FROM recording_deletions) "
            "AND run_id IN (SELECT run_id FROM recording_uploads) "
            "ORDER BY recorded_at ASC",
            (cutoff,),
        ).fetchall()
        return [(run_id, Path(path)) for run_id, path in rows]

    def unarchived_recordings_older_than(self, now: datetime, days: int) -> int:
        """How many recordings older than the cutoff have no verified off-site copy yet."""
        cutoff = _iso(now - timedelta(days=days))
        row = self._conn.execute(
            "SELECT COUNT(*) FROM recordings "
            "WHERE recorded_at < ? AND run_id NOT IN (SELECT run_id FROM recording_deletions) "
            "AND run_id NOT IN (SELECT run_id FROM recording_uploads)",
            (cutoff,),
        ).fetchone()
        return int(row[0])

    def archive_ready_days(self, today: date) -> list[date]:
        """UTC days before `today` with at least one run, no run still `started`, and no
        archive row yet, oldest first."""
        rows = self._conn.execute(
            "SELECT substr(started_at, 1, 10) AS day FROM runs "
            "WHERE day < ? AND day NOT IN (SELECT day FROM archive_days) "
            "GROUP BY day HAVING SUM(status = 'started') = 0 ORDER BY day ASC",
            (today.isoformat(),),
        ).fetchall()
        return [date.fromisoformat(row[0]) for row in rows]

    def day_recordings(self, day: date) -> list[DayRecording]:
        """The not-deleted recordings of runs that started on `day`, by run id."""
        rows = self._conn.execute(
            "SELECT recordings.run_id, recordings.path, recordings.sha256, runs.started_at, "
            "recordings.recorded_at, runs.qty "
            "FROM recordings JOIN runs ON runs.id = recordings.run_id "
            "WHERE substr(runs.started_at, 1, 10) = ? "
            "AND recordings.run_id NOT IN (SELECT run_id FROM recording_deletions) "
            "ORDER BY recordings.run_id ASC",
            (day.isoformat(),),
        ).fetchall()
        return [
            DayRecording(run_id, Path(path), sha256, _parse_iso(started), _parse_iso(recorded), qty)
            for run_id, path, sha256, started, recorded, qty in rows
        ]

    def day_fills(self, day: date) -> list[FillRow]:
        """Every fill of the runs that started on `day`, by fill id."""
        rows = self._conn.execute(
            "SELECT fills.id, fills.run_id, fills.algo, fills.venue, fills.qty, fills.price, "
            "fills.fee, fills.ts_ns "
            "FROM fills JOIN runs ON runs.id = fills.run_id "
            "WHERE substr(runs.started_at, 1, 10) = ? ORDER BY fills.id ASC",
            (day.isoformat(),),
        ).fetchall()
        return [FillRow(*row) for row in rows]

    def record_archive_day(
        self,
        day: date,
        commit_oid: str,
        manifest_path: str,
        manifest_sha256: str,
        fills_path: str,
        fills_sha256: str,
        fills_rows: int,
        uploads: Sequence[RecordingUpload],
        uploaded_at: datetime,
    ) -> None:
        """Record a verified off-site commit for `day`: its ledger row and one row per upload,
        in one transaction."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO archive_days (day, commit_oid, manifest_path, manifest_sha256, "
                "fills_path, fills_sha256, fills_rows, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day.isoformat(),
                    commit_oid,
                    manifest_path,
                    manifest_sha256,
                    fills_path,
                    fills_sha256,
                    fills_rows,
                    _iso(uploaded_at),
                ),
            )
            archive_day_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO recording_uploads (run_id, archive_day_id, path_in_repo, sha256, "
                "bytes) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        upload.run_id,
                        archive_day_id,
                        upload.path_in_repo,
                        upload.sha256,
                        upload.bytes,
                    )
                    for upload in uploads
                ],
            )

    def delete_recordings(self, run_ids: Sequence[int], now: datetime) -> None:
        """Record that the recordings for `run_ids` were deleted. The caller removes the files;
        this only appends the audit trail (see `recording_deletions` in the initial migration).
        """
        if not run_ids:
            return
        deleted_at = _iso(now)
        with self._conn:
            self._conn.executemany(
                "INSERT INTO recording_deletions (run_id, deleted_at) VALUES (?, ?)",
                [(run_id, deleted_at) for run_id in run_ids],
            )

    def backup(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup_conn = sqlite3.connect(dest)
        try:
            self._conn.backup(backup_conn)
        finally:
            backup_conn.close()
