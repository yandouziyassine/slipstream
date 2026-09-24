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

from slipstream.config import ConfigError, load_settings
from slipstream.engine_client import EngineClient, EngineError
from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import fetch_ohlc
from slipstream.live import LiveFeedError, run_live
from slipstream.logging_setup import configure_logging
from slipstream.models import OrderSpec
from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header
from slipstream.replay import ReplayError, read_replay, run_replay
from slipstream.runner import ExecutionRunner, OrderRejectedError
from slipstream.v1 import execution_pb2 as pb

_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEADLINE_GRACE_S = 60


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slipstream",
        description="Paper-trade a TWAP order against the live or a recorded Kraken order book.",
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
    for sub in (live, replay):
        sub.add_argument("--side", choices=["buy", "sell"], required=True)
        sub.add_argument("--qty", type=_positive_float, required=True)
        sub.add_argument("--duration", type=_positive_int, required=True, help="seconds")
        sub.add_argument("--slices", type=_positive_int, required=True)
        sub.add_argument("--symbol", type=_symbol, default="BTC/USD")
        sub.add_argument("--order-id", type=_order_id, default=None)
        sub.add_argument("--engine", default=None, help="engine loopback host:port")
    return parser


def format_summary(status: pb.OrderStatus) -> str:
    state = pb.OrderState.Name(status.state).removeprefix("ORDER_STATE_")
    lines = [
        f"order        {status.order_id}",
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
        client = EngineClient(args.engine or settings.engine_address)
    except ConfigError as exc:
        log.error(str(exc))
        return 1
    try:
        client.wait_ready()
        spec = OrderSpec(
            order_id=args.order_id or f"twap-{time.time_ns()}",
            side=args.side,
            qty=args.qty,
            duration_s=args.duration,
            num_slices=args.slices,
        )
        runner = ExecutionRunner(client, spec, args.symbol, log)
        if args.command == "replay":
            run_replay(runner, read_replay(args.file))
        else:
            deadline_s = args.duration + _DEADLINE_GRACE_S
            asyncio.run(run_live(runner, args.symbol, args.depth, deadline_s=deadline_s))
        status = runner.order_status()
        if status is None:
            log.error("order was never submitted (no order book snapshot received)")
            return 1
        print(format_summary(status))
        return 0
    except (
        EngineError,
        KrakenMessageError,
        ReplayError,
        OrderRejectedError,
        LiveFeedError,
        OSError,
    ) as exc:
        log.error(str(exc))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
