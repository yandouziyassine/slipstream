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
from slipstream.config import ConfigError, load_settings
from slipstream.engine_client import EngineClient, EngineError
from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import fetch_ohlc, parse_ohlc
from slipstream.live import LiveFeedError, run_live
from slipstream.logging_setup import configure_logging
from slipstream.models import Fill, OrderSpec
from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header
from slipstream.replay import ReplayError, read_calibration, read_replay, run_replay
from slipstream.runner import ExecutionRunner, OrderRejectedError
from slipstream.v1 import execution_pb2 as pb

_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEADLINE_GRACE_S = 60
ALGOS = ("twap", "vwap", "pov", "almgren_chriss")
_CALIBRATED = ("vwap", "almgren_chriss")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slipstream",
        description=(
            "Paper-trade execution schedules against the live or a recorded Kraken order book."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="stream the live Kraken book (paper fills only)")
    live.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)
    replay = commands.add_parser("replay", help="replay a recorded JSONL order book file")
    replay.add_argument("--file", type=Path, required=True)
    record = commands.add_parser("record", help="record the live Kraken book + trades to JSONL")
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
    return parser


def format_summary(status: pb.OrderStatus) -> str:
    state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
    lines = [
        f"order        {status.order_id}",
        f"algo         {status.algo or 'twap'}",
        f"state        {state}",
        f"filled       {status.filled_qty:.8g} / {status.total_qty:.8g}",
        f"avg price    {status.avg_fill_price:.2f}",
        f"arrival mid  {status.arrival_mid:.2f}",
        f"slippage     {status.slippage_bps:.2f} bps",
        f"one-shot     {status.immediate_cost_bps:.2f} bps (single market order at arrival)",
        f"saved        {status.immediate_cost_bps - status.slippage_bps:.2f} bps",
    ]
    if status.halt_reason:
        lines.append(f"halt reason  {status.halt_reason}")
    return "\n".join(lines)


def format_comparison(statuses: Sequence[pb.OrderStatus], fills: Sequence[Fill]) -> str:
    rows = [
        f"{'algo':<16}{'state':<11}{'filled':>10}{'avg px':>12}{'slip bps':>10}"
        f"{'1-shot bps':>12}{'saved bps':>11}{'fills':>7}"
    ]
    for status in statuses:
        state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
        count = sum(1 for fill in fills if fill.order_id == status.order_id)
        saved = status.immediate_cost_bps - status.slippage_bps
        rows.append(
            f"{status.algo:<16}{state:<11}{status.filled_qty:>10.8g}"
            f"{status.avg_fill_price:>12.2f}{status.slippage_bps:>10.2f}"
            f"{status.immediate_cost_bps:>12.2f}{saved:>11.2f}{count:>7}"
        )
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
            count = asyncio.run(record_stream(handle, args.symbol, args.depth, args.duration))
    except (RecordError, KrakenMessageError, OSError) as exc:
        log.error(str(exc))
        return 1
    log.info(
        "recording complete",
        extra={"fields": {"event": "record", "messages": count, "path": str(args.out)}},
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = configure_logging()
    if args.command == "record":
        return _record(args, log)
    try:
        settings = load_settings()
        specs = _order_specs(args)
        calibration = _load_calibration(args, specs)
        client = EngineClient(args.engine or settings.engine_address)
    except (ConfigError, CalibrationError, KrakenMessageError, ReplayError, OSError) as exc:
        log.error(str(exc))
        return 1
    try:
        client.wait_ready()
        runner = ExecutionRunner(client, specs, args.symbol, log, calibration)
        replay_file = getattr(args, "file", None)
        if replay_file is not None:
            run_replay(runner, read_replay(replay_file))
        else:
            deadline_s = args.duration + _DEADLINE_GRACE_S
            asyncio.run(run_live(runner, args.symbol, args.depth, deadline_s=deadline_s))
        statuses = runner.order_statuses()
        if not statuses:
            log.error("order was never submitted (no order book snapshot received)")
            return 1
        if args.command == "compare":
            print(format_comparison(statuses, runner.fills))
        else:
            print(format_summary(statuses[0]))
        return 0
    except (
        EngineError,
        KrakenMessageError,
        ReplayError,
        OrderRejectedError,
        LiveFeedError,
        CalibrationError,
        OSError,
    ) as exc:
        log.error(str(exc))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
