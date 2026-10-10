from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    contextmanager,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from slipstream.calibration import CalibrationData, CalibrationError
from slipstream.db import ResultsDB
from slipstream.engine_process import (
    EngineConfigError,
    EngineStartError,
    RunningEngine,
    running_engine,
    validate_engine_binary,
    validate_venue_flag,
)
from slipstream.engine_stream import EngineChannel, EngineError
from slipstream.kraken_rest import fetch_ohlc, parse_ohlc
from slipstream.live import LiveFeedError
from slipstream.logging_setup import JsonFormatter
from slipstream.models import Algo, MarketDataError, OrderSpec, Side, Venue
from slipstream.recorder import RecordError, record_stream
from slipstream.session import LiveSession, OrderRejectedError, check_clock_mode
from slipstream.storage import (
    StorageConfigError,
    StoragePolicy,
    daily_backup,
    disk_usage_mb,
    enforce_recordings_budget,
    expire_recordings,
    prune_old_logs,
)
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules, fetch_venue_rules
from slipstream.venue_ws import FeedReconnect

ALGOS: tuple[Algo, ...] = ("twap", "vwap", "pov", "almgren_chriss")

# Paper risk limits of each size run's engine. Every size gets a fresh engine, so the position
# limit only has to hold one size's orders: all algorithms at the largest size, plus headroom.
HOURLY_MAX_ORDER_NOTIONAL = 50000
HOURLY_MAX_POSITION = 1.5

_BUILD_DIR = Path(__file__).resolve().parents[2] / "build"

# Errors that can happen once a run is in progress: recorded as a failed run, never raised out.
_RUN_ERRORS = (
    EngineError,
    MarketDataError,
    OrderRejectedError,
    RecordError,
    LiveFeedError,
    OSError,
)
# Errors that stop one size's engine from becoming usable: recorded as that size's failed run.
_ENGINE_SETUP_ERRORS = (EngineStartError, EngineError)
# Errors that mean the hour never got started (venue rules or calibration data unreachable):
# these must propagate, since there is no run row yet to attach them to.
_HOUR_SETUP_ERRORS = (MarketDataError, CalibrationError, OSError)

EngineLauncher = Callable[[float], AbstractAsyncContextManager[RunningEngine]]


def _default_data_dir() -> Path:
    raw = os.environ.get("SLIPSTREAM_DATA_DIR")
    return Path(raw) if raw else Path.home() / "slipstream-data"


@dataclass(frozen=True)
class CollectConfig:
    sizes: tuple[float, ...] = (0.01, 0.25)
    duration_s: int = 600
    slices: int = 10
    participation: float = 0.1
    venues: tuple[Venue, ...] = ("kraken", "coinbase")
    symbol: str = "BTC/USD"
    book_depth: int = 10
    data_dir: Path = field(default_factory=_default_data_dir)


def side_for(hour_utc: int) -> Side:
    return "buy" if hour_utc % 2 == 0 else "sell"


def acquire_lock(data_dir: Path) -> AbstractContextManager[None] | None:
    """A non-blocking lock over one hourly run. Returns None when another run holds it."""
    handle = (data_dir / "collect.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None

    @contextmanager
    def _held() -> Iterator[None]:
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    return _held()


def _qty_token(value: float) -> str:
    # A qty like 0.01 or 0.25 as a token safe for order ids (engine allows only
    # alphanumerics, '-' and '_') and for file names: "0p01", "0p25".
    text = format(value, ".12f").rstrip("0").rstrip(".")
    text = text if text else "0"
    return text.replace(".", "p")


def _order_id(now: datetime, size: float, algo: str) -> str:
    return f"h{now:%Y%m%d%H}-{_qty_token(size)}-{algo}"


def _order_specs(now: datetime, side: Side, size: float, cfg: CollectConfig) -> list[OrderSpec]:
    return [
        OrderSpec(
            order_id=_order_id(now, size, algo),
            side=side,
            qty=size,
            duration_s=cfg.duration_s,
            num_slices=cfg.slices,
            algo=algo,
            participation=cfg.participation,
        )
        for algo in ALGOS
    ]


def _recording_path(data_dir: Path, now: datetime, size: float) -> Path:
    return data_dir / "recordings" / f"{now:%Y-%m-%d}" / f"{now:%H}-{_qty_token(size)}.jsonl.gz"


def _recorder_reconnect_logger(
    log: logging.Logger, reconnects: list[FeedReconnect]
) -> Callable[[FeedReconnect], None]:
    def record(event: FeedReconnect) -> None:
        reconnects.append(event)
        log.warning(
            "recorder reconnect",
            extra={
                "fields": {
                    "event": "recorder_reconnect",
                    "venue": event.venue,
                    "attempt": event.attempt,
                    "reason": event.reason,
                    "downtime_s": round(event.downtime_s, 3),
                    "recovered": event.recovered,
                }
            },
        )

    return record


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


async def _run_size(
    channel: EngineChannel,
    engine_address: str,
    cfg: CollectConfig,
    now: datetime,
    side: Side,
    size: float,
    fees: Mapping[Venue, float],
    venue_rules: Mapping[Venue, VenueRules],
    calibration: CalibrationData,
    git_commit: str | None,
    db: ResultsDB,
    log: logging.Logger,
    urls: Mapping[Venue, str] | None,
) -> int:
    run_id = db.begin_run(now, side, size, cfg.duration_s, fees, venue_rules, git_commit)
    rec_path = _recording_path(cfg.data_dir, now, size)
    specs = _order_specs(now, side, size, cfg)
    exc: BaseException | None = None
    session: LiveSession | None = None
    recorder_reconnects: list[FeedReconnect] = []
    try:
        rec_path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(rec_path, "wt", encoding="utf-8") as handle:
            session = LiveSession(
                channel,
                specs,
                cfg.symbol,
                log,
                calibration,
                venues=cfg.venues,
                fee_bps=fees,
                book_depth=cfg.book_depth,
            )
            async with asyncio.TaskGroup() as group:
                record_task = group.create_task(
                    record_stream(
                        handle,
                        cfg.symbol,
                        cfg.book_depth,
                        cfg.duration_s,
                        venues=cfg.venues,
                        urls=urls,
                        on_reconnect=_recorder_reconnect_logger(log, recorder_reconnects),
                    )
                )
                session_task = group.create_task(session.run(engine_address, urls=urls))
    except* _RUN_ERRORS as errors:
        exc = errors.exceptions[0]
    db.add_reconnects(run_id, "feed", session.reconnects if session is not None else [])
    db.add_reconnects(run_id, "recorder", recorder_reconnects)
    if exc is not None:
        _fail_run(db, log, run_id, size, exc)
        return run_id
    record_task.result()
    result = session_task.result()
    sha256 = _sha256_file(rec_path)
    db.set_recording(run_id, rec_path, sha256, datetime.now(UTC))
    db.add_results(run_id, result.statuses, result.fills, result.stats)
    db.finish_run(run_id, "completed", datetime.now(UTC))
    log.info(
        f"run {run_id} completed",
        extra={"fields": {"event": "run_completed", "run_id": run_id, "size": size}},
    )
    return run_id


def _fail_run(
    db: ResultsDB, log: logging.Logger, run_id: int, size: float, exc: BaseException
) -> None:
    log.error(
        f"run {run_id} failed: {exc}",
        extra={"fields": {"event": "run_failed", "run_id": run_id, "size": size}},
    )
    db.finish_run(run_id, "failed", datetime.now(UTC), error=str(exc)[:500])


async def _run_size_on_own_engine(
    launch: EngineLauncher,
    cfg: CollectConfig,
    now: datetime,
    side: Side,
    size: float,
    venue_rules: Mapping[Venue, VenueRules],
    calibration: CalibrationData,
    git_commit: str | None,
    db: ResultsDB,
    log: logging.Logger,
    urls: Mapping[Venue, str] | None,
) -> int:
    """One size run on an engine of its own, so no order, position or statistic left over by an
    earlier size (e.g. orders still working after that run failed) can leak into this one."""
    async with AsyncExitStack() as stack:
        try:
            engine = await stack.enter_async_context(launch(size))
            log.info(
                f"engine started on {engine.address}",
                extra={"fields": {"event": "engine_started", "size": size, "pid": engine.pid}},
            )
            channel = EngineChannel(engine.address)
            stack.push_async_callback(channel.close)
            await channel.wait_ready()
            await check_clock_mode(channel, pb.CLOCK_MODE_LIVE)
            fees = await channel.venue_fees()
        except _ENGINE_SETUP_ERRORS as exc:
            run_id = db.begin_run(now, side, size, cfg.duration_s, {}, venue_rules, git_commit)
            _fail_run(db, log, run_id, size, exc)
            return run_id
        return await _run_size(
            channel,
            engine.address,
            cfg,
            now,
            side,
            size,
            fees,
            venue_rules,
            calibration,
            git_commit,
            db,
            log,
            urls,
        )


def _fetch_live_calibration(symbol: str) -> CalibrationData:
    return CalibrationData(parse_ohlc(fetch_ohlc(symbol, 15)), parse_ohlc(fetch_ohlc(symbol, 1)))


async def run_hour(
    launch: EngineLauncher,
    cfg: CollectConfig,
    now: datetime,
    git_commit: str | None,
    db: ResultsDB,
    log: logging.Logger,
    urls: Mapping[Venue, str] | None = None,
    fetch_rules: Callable[[Sequence[Venue], str], dict[Venue, VenueRules]] = fetch_venue_rules,
    fetch_calibration: Callable[[str], CalibrationData] = _fetch_live_calibration,
) -> list[int]:
    side = side_for(now.hour)
    venue_rules = fetch_rules(cfg.venues, cfg.symbol)
    calibration = fetch_calibration(cfg.symbol)
    return [
        await _run_size_on_own_engine(
            launch, cfg, now, side, size, venue_rules, calibration, git_commit, db, log, urls
        )
        for size in cfg.sizes
    ]


def engine_argv(binary: Path, venue_flags: Sequence[str]) -> list[str]:
    argv = [
        str(binary),
        "--listen",
        "127.0.0.1:0",
        "--clock",
        "live",
        "--max-order-notional",
        str(HOURLY_MAX_ORDER_NOTIONAL),
        "--max-position",
        str(HOURLY_MAX_POSITION),
    ]
    for flag in venue_flags:
        argv += ["--venue", flag]
    return argv


def engine_launcher(argv: Sequence[str], log_dir: Path, now: datetime) -> EngineLauncher:
    def launch(size: float) -> AbstractAsyncContextManager[RunningEngine]:
        log_path = log_dir / f"engine-{now:%Y-%m-%d-%H%M%S}-{_qty_token(size)}.log"
        return running_engine(argv, log_path)

    return launch


def _raise_exit(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def sigterm_exits() -> Iterator[None]:
    """Turns SIGTERM into SystemExit, so `finally` blocks still stop the engine of the size run
    in progress instead of leaving it running as an orphan."""
    previous = signal.signal(signal.SIGTERM, _raise_exit)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _git_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root or Path(__file__).resolve().parents[2]
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
            [git, "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _housekeeping(
    cfg: CollectConfig,
    policy: StoragePolicy,
    db: ResultsDB,
    now: datetime,
    log: logging.Logger,
) -> None:
    """Everything that keeps `$DATA` bounded. Called once per hour, after the live runs finish,
    so it never competes with the engine or the feeds for CPU or disk I/O (see `main`, which
    lowers this process's priority right before calling in).
    """
    created = daily_backup(db, cfg.data_dir, now, policy.backups_to_keep)
    if created is not None:
        log.info(
            f"backup created: {created.name}",
            extra={"fields": {"event": "backup_created", "path": created.name}},
        )
    if now.hour == 0:
        thinned = db.thin_recordings(
            now, keep_hour_utc=policy.thin_keep_hour_utc, older_than_days=policy.full_retention_days
        )
        for path in thinned:
            path.unlink(missing_ok=True)
        if thinned:
            log.info(
                f"thinned {len(thinned)} recording(s)",
                extra={"fields": {"event": "recordings_thinned", "count": len(thinned)}},
            )
        expired = expire_recordings(db, now, policy.thin_retention_days)
        for path in expired:
            path.unlink(missing_ok=True)
        if expired:
            log.info(
                f"expired {len(expired)} recording(s)",
                extra={"fields": {"event": "recordings_expired", "count": len(expired)}},
            )
        pruned_logs = prune_old_logs(cfg.data_dir, now, policy.logs_max_age_days)
        if pruned_logs:
            log.info(
                f"pruned {pruned_logs} old log file(s)",
                extra={"fields": {"event": "logs_pruned", "count": pruned_logs}},
            )
    protect = frozenset(_recording_path(cfg.data_dir, now, size) for size in cfg.sizes)
    evicted_paths, evicted_mb = enforce_recordings_budget(db, cfg.data_dir, policy, now, protect)
    for path in evicted_paths:
        path.unlink(missing_ok=True)
    if evicted_paths:
        log.warning(
            f"deleted {len(evicted_paths)} recording(s) ({evicted_mb:.1f} MB) over disk budget",
            extra={
                "fields": {
                    "event": "recordings_budget_evicted",
                    "count": len(evicted_paths),
                    "mb": round(evicted_mb, 1),
                }
            },
        )
    usage = disk_usage_mb(cfg.data_dir, cfg.data_dir / "slipstream.db")
    log.info("disk usage", extra={"fields": {"event": "disk_usage", **usage}})


def _configure_file_logging(path: Path) -> logging.Logger:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("slipstream.collect")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _engine_binary(value: str) -> Path:
    try:
        return validate_engine_binary(Path(value), _BUILD_DIR)
    except EngineConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _venue_flag(value: str) -> str:
    try:
        return validate_venue_flag(value)
    except EngineConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slipstream.collect")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="collect one hour of paper-trading evidence")
    run.add_argument(
        "--engine-binary",
        required=True,
        type=_engine_binary,
        help="slipstream_engine under the repo's build/ dir; one is started per size run",
    )
    run.add_argument(
        "--venue",
        dest="venue_flags",
        action="append",
        required=True,
        type=_venue_flag,
        help="engine --venue value as printed by `slipstream.cli venue-flags`; one per venue",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = CollectConfig()
    flag_venues = sorted(flag.split(":", 1)[0] for flag in args.venue_flags)
    if flag_venues != sorted(cfg.venues):
        parser.error(f"--venue must be given once for each of {', '.join(cfg.venues)}")
    cfg.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.data_dir.chmod(0o700)
    (cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    log = _configure_file_logging(cfg.data_dir / "logs" / f"collect-{now:%Y-%m-%d}.log")

    try:
        policy = StoragePolicy.from_env()
    except StorageConfigError as exc:
        log.error(str(exc), extra={"fields": {"event": "invalid_storage_policy"}})
        return 1

    lock = acquire_lock(cfg.data_dir)
    if lock is None:
        log.info("skipped: previous run active", extra={"fields": {"event": "skipped"}})
        return 0

    with lock:
        db = ResultsDB(cfg.data_dir / "slipstream.db")
        try:
            db.migrate()
            abandoned = db.mark_abandoned(now)
            if abandoned:
                log.warning(
                    f"marked {abandoned} run(s) abandoned",
                    extra={"fields": {"event": "abandoned", "count": abandoned}},
                )
            launch = engine_launcher(
                engine_argv(args.engine_binary, args.venue_flags), cfg.data_dir / "logs", now
            )
            try:
                with sigterm_exits():
                    run_ids = asyncio.run(run_hour(launch, cfg, now, _git_commit(), db, log))
            except _HOUR_SETUP_ERRORS as exc:
                log.error(str(exc), extra={"fields": {"event": "run_hour_failed"}})
                return 1
            log.info(
                f"collected {len(run_ids)} run(s)",
                extra={"fields": {"event": "collected", "run_ids": run_ids}},
            )
            # The live runs are done, so housekeeping (backup, thinning, budget enforcement) can
            # run at low priority without touching the latency we just measured.
            if hasattr(os, "nice"):
                os.nice(policy.housekeeping_nice)
            _housekeeping(cfg, policy, db, now, log)
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
