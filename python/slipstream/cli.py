from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from slipstream.calibration import CalibrationData, CalibrationError
from slipstream.collect import main as collect_main
from slipstream.config import ConfigError, load_settings, validate_engine_address
from slipstream.engine_stream import EngineChannel, EngineError
from slipstream.kraken_rest import fetch_ohlc, parse_ohlc
from slipstream.live import LiveFeedError
from slipstream.logging_setup import configure_logging
from slipstream.models import Fill, MarketDataError, OrderSpec, Venue
from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header
from slipstream.replay import ReplayError, read_calibration, read_replay
from slipstream.session import LiveSession, OrderRejectedError, ReplaySession, check_clock_mode
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules, VenueRulesError, fetch_venue_rules
from slipstream.venues.registry import VENUES

_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ALGOS = ("twap", "vwap", "pov", "almgren_chriss")
_CALIBRATED = ("vwap", "almgren_chriss")
_DEPTH_CHECKED_COMMANDS = ("live", "compare")
_FULL_FILL_TOL = 1e-9


def _positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {value!r}")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value!r}")
    return result


def _symbol(value: str) -> str:
    if not _SYMBOL.match(value):
        raise argparse.ArgumentTypeError("symbol must look like BTC/USD")
    return value


def _order_id(value: str) -> str:
    if not _ORDER_ID.match(value):
        raise argparse.ArgumentTypeError("order id: 1-64 chars of A-Z a-z 0-9 _ -")
    return value


def _participation(value: str) -> float:
    result = _positive_float(value)
    if result > 0.5:
        raise argparse.ArgumentTypeError("participation must be at most 0.5")
    return result


def _algos(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in ALGOS]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"algos must be a comma list of {', '.join(ALGOS)}")
    return list(dict.fromkeys(names))


def _venues(value: str) -> tuple[Venue, ...]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in VENUES]
    if not names or unknown:
        raise argparse.ArgumentTypeError(f"venues must be a comma list of {', '.join(VENUES)}")
    return tuple(dict.fromkeys(names))


def _fees(value: str) -> dict[Venue, float]:
    fees: dict[Venue, float] = {}
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    if not entries:
        raise argparse.ArgumentTypeError(f"fees must be a comma list of {', '.join(VENUES)}=bps")
    for entry in entries:
        name, sep, raw = entry.partition("=")
        if not sep or not raw or name not in VENUES:
            raise argparse.ArgumentTypeError(
                f"fees must be a comma list of {', '.join(VENUES)}=bps"
            )
        if name in fees:
            raise argparse.ArgumentTypeError(f"duplicate fee for {name!r}")
        try:
            bps = float(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"fee for {name!r} is not a number") from exc
        if not math.isfinite(bps) or not 0 <= bps <= 1000:
            raise argparse.ArgumentTypeError(f"fee for {name!r} must be in [0, 1000]")
        fees[name] = bps
    return fees


def _plain(value: float) -> str:
    # format(x, "f") only keeps 6 decimal digits by default, which rounds a qty_step of
    # 1e-08 down to zero. 12 digits covers Kraken's lot_decimals (0..12) without exponents.
    text = format(value, ".12f").rstrip("0").rstrip(".")
    return text if text else "0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slipstream",
        description=(
            "Paper-trade execution schedules across Kraken and Coinbase public order books "
            "(live or recorded)."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="stream live public order books (paper fills only)")
    live.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)
    replay = commands.add_parser("replay", help="replay a recorded JSONL order book file")
    replay.add_argument("--file", type=Path, required=True)
    record = commands.add_parser("record", help="record live public order books + trades to JSONL")
    record.add_argument("--duration", type=_positive_int, required=True, help="seconds")
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--symbol", type=_symbol, default="BTC/USD")
    record.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)
    compare = commands.add_parser("compare", help="run several algorithms side by side on one feed")
    compare.add_argument("--algos", type=_algos, default=list(ALGOS))
    compare.add_argument(
        "--file", type=Path, default=None, help="replay file; live feed when omitted"
    )
    compare.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)
    for sub in (live, replay, compare):
        sub.add_argument("--side", choices=["buy", "sell"], required=True)
        sub.add_argument("--qty", type=_positive_float, required=True)
        sub.add_argument("--duration", type=_positive_int, required=True, help="seconds")
        sub.add_argument("--slices", type=_positive_int, required=True)
        sub.add_argument("--symbol", type=_symbol, default="BTC/USD")
        sub.add_argument("--engine", default=None, help="engine loopback host:port")
        sub.add_argument("--urgency", choices=["low", "medium", "high"], default="medium")
        sub.add_argument("--risk-aversion", type=_positive_float, default=None)
        sub.add_argument("--participation", type=_participation, default=0.1)
    for sub in (live, replay):
        sub.add_argument("--algo", choices=ALGOS, default="twap")
        sub.add_argument("--order-id", type=_order_id, default=None)
    for sub in (live, replay, compare, record):
        sub.add_argument(
            "--venues",
            type=_venues,
            default=("kraken",),
            help="comma list of venues; must match the engine's --venue flags",
        )
    venue_flags = commands.add_parser(
        "venue-flags", help="fetch venue trading rules and print engine --venue flags"
    )
    venue_flags.add_argument("--venues", type=_venues, default=("kraken",))
    venue_flags.add_argument("--fees", type=_fees, required=True, help="kraken=40,coinbase=60")
    venue_flags.add_argument("--symbol", type=_symbol, default="BTC/USD")
    collect = commands.add_parser(
        "collect", help="hourly paper-trading evidence collector (see slipstream.collect)"
    )
    collect.add_argument("collect_args", nargs=argparse.REMAINDER)
    return parser


def routing_gain_bps(status: pb.OrderStatus) -> float | None:
    available = [cost.all_in_bps for cost in status.venue_costs if cost.available]
    if not available:
        return None
    return min(available) - status.routed_all_in_bps


def _num(value: float | None) -> str:
    # Adding 0.0 turns a rounded -0.0 (float noise such as -1e-13) into 0.0, so it prints "0.00".
    return "n/a" if value is None else f"{round(value, 2) + 0.0:.2f}"


def _bps(value: float | None) -> str:
    return "n/a" if value is None else f"{_num(value)} bps"


def _fully_filled(status: pb.OrderStatus) -> bool:
    threshold = status.total_qty * (1 - _FULL_FILL_TOL)
    return status.filled_qty >= threshold and status.immediate_filled_qty >= threshold


def saved_bps(status: pb.OrderStatus) -> float | None:
    """immediate_cost_bps - slippage_bps, but only when both the order and the one-shot
    benchmark could actually fill in full; a partial fill makes the comparison meaningless."""
    if not _fully_filled(status):
        return None
    return status.immediate_cost_bps - status.slippage_bps


def _filled_pct(status: pb.OrderStatus) -> float:
    if status.total_qty <= 0:
        return 0.0
    return status.filled_qty / status.total_qty * 100.0


def format_summary(status: pb.OrderStatus) -> str:
    state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
    lines = [
        f"order        {status.order_id}",
        f"algo         {status.algo or 'twap'}",
        f"state        {state}",
        f"filled       {status.filled_qty:.8g} / {status.total_qty:.8g}",
        f"filled %     {_filled_pct(status):.2f}%",
        f"avg price    {status.avg_fill_price:.2f}",
        f"arrival mid  {status.arrival_mid:.2f}",
        f"slippage     {status.slippage_bps:.2f} bps",
        f"one-shot     {status.immediate_cost_bps:.2f} bps (single market order at arrival)",
        f"saved        {_bps(saved_bps(status))}",
    ]
    lines += [
        f"fees         {status.fees_bps:.2f} bps ({status.fees_paid:.2f} paid)",
        f"all-in       {status.routed_all_in_bps:.2f} bps (slippage + fees, routed)",
    ]
    if len(status.venue_costs) > 1:
        for cost in status.venue_costs:
            alone = cost.all_in_bps if cost.available else None
            lines.append(f"{'  ' + cost.venue:<13}{_bps(alone)} (all-in on this venue alone)")
        lines.append(f"routing gain {_bps(routing_gain_bps(status))} (vs best single venue)")
    if status.halt_reason:
        lines.append(f"halt reason  {status.halt_reason}")
    return "\n".join(lines)


def format_comparison(statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]) -> str:
    venues = [cost.venue for cost in statuses[0].venue_costs] if statuses else []
    multi = len(venues) > 1
    show_reason = any(status.halt_reason for status in statuses)
    header = (
        f"{'algo':<16}{'state':<11}{'filled':>10}{'filled %':>10}{'avg px':>12}{'slip bps':>10}"
        f"{'1-shot bps':>12}{'saved bps':>11}{'fee bps':>9}{'all-in bps':>12}"
    )
    if multi:
        header += "".join(f"{venue + ' bps':>14}" for venue in venues) + f"{'gain bps':>10}"
    header += f"{'fills':>7}"
    if show_reason:
        header += f"{'reason':>20}"
    rows = [header]
    for status in statuses:
        state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
        count = sum(1 for fill in fills if fill.order_id == status.order_id)
        row = (
            f"{status.algo:<16}{state:<11}{status.filled_qty:>10.8g}"
            f"{_filled_pct(status):>9.2f}%{status.avg_fill_price:>12.2f}"
            f"{status.slippage_bps:>10.2f}{status.immediate_cost_bps:>12.2f}"
            f"{_num(saved_bps(status)):>11}{status.fees_bps:>9.2f}{status.routed_all_in_bps:>12.2f}"
        )
        if multi:
            row += "".join(
                f"{_num(cost.all_in_bps if cost.available else None):>14}"
                for cost in status.venue_costs
            )
            row += f"{_num(routing_gain_bps(status)):>10}"
        row += f"{count:>7}"
        if show_reason:
            row += f"{(status.halt_reason or '-'):>20}"
        rows.append(row)
    return "\n".join(rows)


def _order_specs(args: argparse.Namespace) -> list[OrderSpec]:
    stamp = time.time_ns()
    is_compare = args.command == "compare"
    algos = args.algos if is_compare else [args.algo]
    return [
        OrderSpec(
            order_id=(
                f"cmp-{algo}-{stamp}" if is_compare else (args.order_id or f"{algo}-{stamp}")
            ),
            side=args.side,
            qty=args.qty,
            duration_s=args.duration,
            num_slices=args.slices,
            algo=algo,
            urgency=args.urgency,
            risk_aversion=args.risk_aversion,
            participation=args.participation,
        )
        for algo in algos
    ]


def _load_calibration(
    args: argparse.Namespace, specs: Sequence[OrderSpec]
) -> CalibrationData | None:
    if not any(spec.algo in _CALIBRATED for spec in specs):
        return None
    replay_file = getattr(args, "file", None)
    if replay_file is not None:
        bars = read_calibration(replay_file)
        if 15 not in bars or 1 not in bars:
            raise CalibrationError(
                "replay file has no OHLC calibration data; record it with `slipstream record`"
            )
        return CalibrationData(bars[15], bars[1])
    return CalibrationData(
        parse_ohlc(fetch_ohlc(args.symbol, 15)), parse_ohlc(fetch_ohlc(args.symbol, 1))
    )


def _record(args: argparse.Namespace, log: logging.Logger) -> int:
    try:
        with open_new_file(args.out) as handle:
            for interval in (15, 1):
                write_ohlc_header(handle, interval, fetch_ohlc(args.symbol, interval))
            count = asyncio.run(
                record_stream(handle, args.symbol, args.depth, args.duration, venues=args.venues)
            )
    except (RecordError, MarketDataError, OSError) as exc:
        log.error(str(exc))
        return 1
    log.info(
        "recording complete",
        extra={"fields": {"event": "record", "messages": count, "path": str(args.out)}},
    )
    return 0


def _venue_flags(args: argparse.Namespace, log: logging.Logger) -> int:
    missing = [venue for venue in args.venues if venue not in args.fees]
    if missing:
        log.error(f"missing --fees entries for {', '.join(missing)}")
        return 1
    try:
        rules: dict[Venue, VenueRules] = fetch_venue_rules(args.venues, args.symbol)
    except (VenueRulesError, OSError) as exc:
        log.error(str(exc))
        return 1
    for venue in args.venues:
        r = rules[venue]
        print("--venue")
        print(
            f"{venue}:fee_bps={_plain(args.fees[venue])},min_qty={_plain(r.min_qty)},"
            f"qty_step={_plain(r.qty_step)},min_notional={_plain(r.min_notional)}"
        )
    return 0


def _print_result(
    args: argparse.Namespace, statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]
) -> None:
    if args.command == "compare":
        print(format_comparison(statuses, fills))
    else:
        print(format_summary(statuses[0]))


_RUN_ERRORS = (
    EngineError,
    MarketDataError,
    ReplayError,
    OrderRejectedError,
    LiveFeedError,
    CalibrationError,
    OSError,
)


def _print_best_effort(
    args: argparse.Namespace, statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]
) -> None:
    """Best-effort summary/table after a failure that happened once an order might already be
    working: show whatever the engine reports rather than nothing. Callers only log a failing
    status RPC and skip this."""
    if statuses:
        _print_result(args, statuses, fills)


def _log_status_error(log: logging.Logger, exc: EngineError) -> None:
    log.error(f"could not fetch final order status: {exc}")


def _venues_mismatch(fees: dict[Venue, float], args: argparse.Namespace) -> str | None:
    if set(fees) == set(args.venues):
        return None
    return (
        f"engine venues {sorted(fees)} do not match --venues {sorted(args.venues)}; "
        "start the engine with one --venue flag per venue"
    )


def _depth_mismatch(engine_depth: int, args: argparse.Namespace) -> str | None:
    if args.command not in _DEPTH_CHECKED_COMMANDS or engine_depth <= args.depth:
        return None
    return (
        f"engine book depth {engine_depth} exceeds --depth {args.depth}: levels "
        "outside the subscribed depth would look like phantom liquidity"
    )


def _finish(
    args: argparse.Namespace,
    statuses: Sequence[pb.OrderStatus],
    fills: Sequence[Fill],
    log: logging.Logger,
) -> int:
    if not statuses:
        log.error("order was never submitted (no order book snapshot received)")
        return 1
    _print_result(args, statuses, fills)
    return 0


async def _replay(
    args: argparse.Namespace,
    address: str,
    specs: Sequence[OrderSpec],
    calibration: CalibrationData | None,
    log: logging.Logger,
) -> int:
    channel = EngineChannel(address)
    session: ReplaySession | None = None
    try:
        await channel.wait_ready()
        await check_clock_mode(channel, pb.CLOCK_MODE_REPLAY)
        fees = await channel.venue_fees()
        problem = _venues_mismatch(fees, args)
        if problem is None and args.command in _DEPTH_CHECKED_COMMANDS:
            problem = _depth_mismatch(await channel.book_depth(), args)
        if problem is not None:
            log.error(problem)
            return 1
        session = ReplaySession(
            channel,
            specs,
            args.symbol,
            log,
            calibration,
            venues=args.venues,
            book_depth=getattr(args, "depth", 10),
            fee_bps=fees,
        )
        result = await session.run(read_replay(args.file))
        return _finish(args, result.statuses, result.fills, log)
    except _RUN_ERRORS as exc:
        log.error(str(exc))
        if session is not None:
            try:
                statuses = await session.order_statuses()
            except EngineError as status_exc:
                _log_status_error(log, status_exc)
            else:
                _print_best_effort(args, statuses, session.fills)
        return 1
    finally:
        await channel.close()


def _log_stats(log: logging.Logger, stats: pb.EngineStats) -> None:
    log.info(
        "stats",
        extra={
            "fields": {
                "event": "stats",
                "events": stats.events,
                "latency_p50_us": stats.latency_p50_ns / 1000.0,
                "latency_p99_us": stats.latency_p99_ns / 1000.0,
            }
        },
    )


async def _live(
    args: argparse.Namespace,
    address: str,
    specs: Sequence[OrderSpec],
    calibration: CalibrationData | None,
    log: logging.Logger,
) -> int:
    channel = EngineChannel(address)
    session: LiveSession | None = None
    try:
        await channel.wait_ready()
        await check_clock_mode(channel, pb.CLOCK_MODE_LIVE)
        fees = await channel.venue_fees()
        problem = _venues_mismatch(fees, args)
        if problem is None:
            problem = _depth_mismatch(await channel.book_depth(), args)
        if problem is not None:
            log.error(problem)
            return 1
        session = LiveSession(
            channel,
            specs,
            args.symbol,
            log,
            calibration,
            venues=args.venues,
            book_depth=args.depth,
            fee_bps=fees,
        )
        result = await session.run(address)
        _log_stats(log, result.stats)
        return _finish(args, result.statuses, result.fills, log)
    except _RUN_ERRORS as exc:
        log.error(str(exc))
        if session is not None:
            try:
                statuses = await session.order_statuses()
            except EngineError as status_exc:
                _log_status_error(log, status_exc)
            else:
                _print_best_effort(args, statuses, session.fills)
        return 1
    finally:
        await channel.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "collect":
        return collect_main(args.collect_args)
    log = configure_logging()
    if args.command == "record":
        return _record(args, log)
    if args.command == "venue-flags":
        return _venue_flags(args, log)
    try:
        settings = load_settings()
        specs = _order_specs(args)
        calibration = _load_calibration(args, specs)
        address = args.engine or settings.engine_address
        validate_engine_address(address)
    except (ConfigError, CalibrationError, MarketDataError, ReplayError, OSError) as exc:
        log.error(str(exc))
        return 1
    if getattr(args, "file", None) is not None:
        return asyncio.run(_replay(args, address, specs, calibration, log))
    return asyncio.run(_live(args, address, specs, calibration, log))


if __name__ == "__main__":
    sys.exit(main())
