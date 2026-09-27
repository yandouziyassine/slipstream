from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from slipstream.calibration import CalibrationData, CalibrationError
from slipstream.db import ResultsDB
from slipstream.engine_stream import EngineChannel, EngineError
from slipstream.kraken_rest import fetch_ohlc, parse_ohlc
from slipstream.live import LiveFeedError
from slipstream.logging_setup import JsonFormatter
from slipstream.models import Algo, MarketDataError, OrderSpec, Side, Venue
from slipstream.recorder import RecordError, record_stream
from slipstream.session import LiveSession, OrderRejectedError, check_clock_mode
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules, fetch_venue_rules

ALGOS: tuple[Algo, ...] = ("twap", "vwap", "pov", "almgren_chriss")

# Errors that can happen once a run is in progress: recorded as a failed run, never raised out.
_RUN_ERRORS = (
    EngineError,
    MarketDataError,
    OrderRejectedError,
    RecordError,
    LiveFeedError,
    OSError,
)
# Errors that mean the hour never got started (no engine, venue rules or calibration data
# unreachable): these must propagate, since there is no run row yet to attach them to.
_HOUR_SETUP_ERRORS = (EngineError, MarketDataError, CalibrationError, OSError)

_BACKUPS_TO_KEEP = 14
# Closing one size's Subscribe stream and the engine noticing the cancellation (it polls every
# 50ms; see service.h's kSubscribePoll) race with the next size opening a new one on the same
# engine connection. Retry a "another subscriber is active" rejection a few times before giving
# up, rather than failing a run over a timing gap that always closes within a couple of polls.
_SUBSCRIBER_CONFLICT_RETRIES = 5
_SUBSCRIBER_CONFLICT_DELAY_S = 0.1


def _is_transient_subscriber_conflict(exc: BaseException) -> bool:
    return isinstance(exc, EngineError) and "another subscriber is active" in str(exc)


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
    failed = False
    exc: BaseException | None = None
    for attempt in range(_SUBSCRIBER_CONFLICT_RETRIES):
        failed = False
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
                        )
                    )
                    session_task = group.create_task(session.run(engine_address, urls=urls))
        except* _RUN_ERRORS as errors:
            exc = errors.exceptions[0]
            failed = True
        if not failed:
            break
        last_attempt = attempt + 1 == _SUBSCRIBER_CONFLICT_RETRIES
        if not last_attempt and exc is not None and _is_transient_subscriber_conflict(exc):
            await asyncio.sleep(_SUBSCRIBER_CONFLICT_DELAY_S)
            continue
        break
    if failed:
        if exc is None:
            raise RuntimeError("unreachable: failed is True but no exception was recorded")
        log.error(
            f"run {run_id} failed: {exc}",
            extra={"fields": {"event": "run_failed", "run_id": run_id, "size": size}},
        )
        db.finish_run(run_id, "failed", datetime.now(UTC), error=str(exc)[:500])
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


def _fetch_live_calibration(symbol: str) -> CalibrationData:
    return CalibrationData(parse_ohlc(fetch_ohlc(symbol, 15)), parse_ohlc(fetch_ohlc(symbol, 1)))


async def run_hour(
    engine_address: str,
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
    channel = EngineChannel(engine_address)
    run_ids: list[int] = []
    try:
        await channel.wait_ready()
        await check_clock_mode(channel, pb.CLOCK_MODE_LIVE)
        fees = await channel.venue_fees()
        venue_rules = fetch_rules(cfg.venues, cfg.symbol)
        calibration = fetch_calibration(cfg.symbol)
        for size in cfg.sizes:
            run_id = await _run_size(
                channel,
                engine_address,
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
            run_ids.append(run_id)
    finally:
        await channel.close()
    return run_ids


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


def _prune_backups(backup_dir: Path, keep: int = _BACKUPS_TO_KEEP) -> None:
    backups = sorted(backup_dir.glob("slipstream-*.db"))
    for stale in backups[:-keep]:
        stale.unlink(missing_ok=True)


def _configure_file_logging(path: Path) -> logging.Logger:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("slipstream.collect")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slipstream.collect")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="collect one hour of paper-trading evidence")
    run.add_argument("--engine", required=True, help="engine loopback host:port")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = CollectConfig()
    cfg.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.data_dir.chmod(0o700)
    (cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    log = _configure_file_logging(cfg.data_dir / "logs" / f"collect-{now:%Y-%m-%d}.log")

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
            try:
                run_ids = asyncio.run(run_hour(args.engine, cfg, now, _git_commit(), db, log))
            except _HOUR_SETUP_ERRORS as exc:
                log.error(str(exc), extra={"fields": {"event": "run_hour_failed"}})
                return 1
            log.info(
                f"collected {len(run_ids)} run(s)",
                extra={"fields": {"event": "collected", "run_ids": run_ids}},
            )
            backup_dir = cfg.data_dir / "backup"
            db.backup(backup_dir / f"slipstream-{now:%Y-%m-%d}.db")
            _prune_backups(backup_dir)
            if now.hour == 0:
                for path in db.thin_recordings(now):
                    path.unlink(missing_ok=True)
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
