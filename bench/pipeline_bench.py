"""Benchmark the concurrent replay pipeline against the release engine.

Streams a deterministic synthetic sequence of book deltas and trades on two venues through one
replay `MarketStreamWriter`, driving a POV order that keeps filling. Reports client-side
send-to-fill latency (p50/p99/p99.9) and events/s, plus the engine's own ingest-to-processed
latency from `StatusReply.stats`. Run directly with `bash scripts/bench_pipeline.sh`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from slipstream.engine_stream import (
    EngineBackpressureError,
    EngineChannel,
    MarketStreamWriter,
    Subscription,
    book_event,
    trade_event,
)
from slipstream.models import BookUpdate, OrderSpec, PovParams, TradeBatch, Venue
from slipstream.v1 import execution_pb2 as pb

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE_BIN = REPO_ROOT / "build" / "release" / "slipstream_engine"
DEFAULT_BASELINE = REPO_ROOT / "bench" / "baseline.json"

SYMBOL = "BTC/USD"
VENUES: tuple[Venue, ...] = ("kraken", "coinbase")
ORDER_ID = "bench-pov"
ORDER_QTY = 0.09
ORDER_DURATION_S = 60
ORDER_SLICES = 10
PARTICIPATION = 0.5

TOTAL_EVENTS = 50_000
TRADE_EVERY = 20
NUM_TRADES = TOTAL_EVENTS // TRADE_EVERY
NUM_LEVELS = 10
BASE_PRICE = 100.0
TICK = 0.01
LEVEL_QTY_RANGE = (1.0, 10.0)
TRADE_QTY_RANGE = (2e-5, 4e-5)
RECV_NS_STEP = 1_000
SEED = 20260926

SEND_TIMEOUT_S = 5.0
WAIT_PROCESSED_TIMEOUT_S = 20.0
WAIT_FILLS_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class BenchResult:
    total_events: int
    sample_count: int
    client_p50_us: float
    client_p99_us: float
    client_p999_us: float
    events_per_s: float
    engine_p50_us: float
    engine_p99_us: float


def _bid_prices() -> list[float]:
    return [round(BASE_PRICE - TICK * (i + 1), 6) for i in range(NUM_LEVELS)]


def _ask_prices() -> list[float]:
    return [round(BASE_PRICE + TICK * (i + 1), 6) for i in range(NUM_LEVELS)]


def _initial_levels(
    rng: random.Random,
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    bids = tuple((price, rng.uniform(*LEVEL_QTY_RANGE)) for price in _bid_prices())
    asks = tuple((price, rng.uniform(*LEVEL_QTY_RANGE)) for price in _ask_prices())
    return bids, asks


def _snapshot_events(rng: random.Random) -> list[tuple[int, pb.MarketEvent]]:
    """One full snapshot per venue, so both books are usable before the order is submitted."""
    events: list[tuple[int, pb.MarketEvent]] = []
    recv_ns = 0
    for venue in VENUES:
        recv_ns += RECV_NS_STEP
        bids, asks = _initial_levels(rng)
        update = BookUpdate(SYMBOL, True, bids, asks, venue)
        events.append((recv_ns, book_event(update, recv_ns)))
    return events


def _stream_events(rng: random.Random, start_recv_ns: int) -> list[tuple[int, pb.MarketEvent]]:
    """50k deterministic events: mostly book deltas within a stable band, some trades.

    Only trades move `market_volume_`, so only they drive POV fills; the deltas keep both books
    fresh and liquid so those fills can actually route.
    """
    bid_prices = _bid_prices()
    ask_prices = _ask_prices()
    events: list[tuple[int, pb.MarketEvent]] = []
    recv_ns = start_recv_ns
    for i in range(TOTAL_EVENTS):
        recv_ns += RECV_NS_STEP
        venue = VENUES[i % len(VENUES)]
        if i % TRADE_EVERY == 0:
            qty = rng.uniform(*TRADE_QTY_RANGE)
            batch = TradeBatch(SYMBOL, False, ((BASE_PRICE, qty),), venue)
            events.append((recv_ns, trade_event(batch, recv_ns)))
        else:
            idx = rng.randrange(NUM_LEVELS)
            qty = rng.uniform(*LEVEL_QTY_RANGE)
            if i % 4 < 2:
                update = BookUpdate(SYMBOL, False, ((bid_prices[idx], qty),), (), venue)
            else:
                update = BookUpdate(SYMBOL, False, (), ((ask_prices[idx], qty),), venue)
            events.append((recv_ns, book_event(update, recv_ns)))
    return events


async def _send(
    writer: MarketStreamWriter, event: pb.MarketEvent, recv_ns: int, send_times: dict[int, int]
) -> None:
    """Sends `event` and waits until it has actually been written to the engine.

    `MarketStreamWriter` buffers up to 1,000 events client-side. Sending faster than they are
    written would make the measured latency include time spent queued in that local buffer rather
    than in the pipeline, so each send waits for the previous one's write to complete.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SEND_TIMEOUT_S
    while True:
        try:
            writer.send_nowait(event)
            break
        except EngineBackpressureError:
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(0.001)
    # Recorded before waiting: the engine can answer with a fill before the write call returns.
    send_times[recv_ns] = time.perf_counter_ns()
    await asyncio.wait_for(writer.drain(), timeout=max(0.0, deadline - loop.time()))


async def _consume(
    subscription: Subscription,
    order_id: str,
    send_times: dict[int, int],
    latency_by_event: dict[int, int],
) -> None:
    async for event in subscription:
        if event.WhichOneof("event") != "fill" or event.fill.order_id != order_id:
            continue
        recv_time = time.perf_counter_ns()
        trigger_ns = event.fill.ts_ns
        if trigger_ns in send_times and trigger_ns not in latency_by_event:
            latency_by_event[trigger_ns] = recv_time - send_times[trigger_ns]


async def _wait_processed(
    channel: EngineChannel, expected_events: int, timeout_s: float = WAIT_PROCESSED_TIMEOUT_S
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        status = await channel.status()
        if status.stats.events >= expected_events:
            return
        if loop.time() >= deadline:
            raise RuntimeError(
                f"engine processed only {status.stats.events}/{expected_events} events "
                f"after {timeout_s:.0f}s"
            )
        await asyncio.sleep(0.01)


async def _wait_for_fills(
    latency_by_event: dict[int, int], min_count: int, timeout_s: float = WAIT_FILLS_TIMEOUT_S
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while len(latency_by_event) < min_count:
        if loop.time() >= deadline:
            raise RuntimeError(
                f"only {len(latency_by_event)}/{min_count} fills observed after {timeout_s:.0f}s"
            )
        await asyncio.sleep(0.02)


def _percentile_us(sorted_samples_ns: list[int], pct: float) -> float:
    if not sorted_samples_ns:
        return float("nan")
    if len(sorted_samples_ns) == 1:
        return sorted_samples_ns[0] / 1000.0
    rank = (len(sorted_samples_ns) - 1) * pct
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return sorted_samples_ns[int(rank)] / 1000.0
    lo_val, hi_val = sorted_samples_ns[lo], sorted_samples_ns[hi]
    return (lo_val + (hi_val - lo_val) * (rank - lo)) / 1000.0


def _start_engine(engine_bin: Path) -> tuple[subprocess.Popen[str], str]:
    if not engine_bin.exists():
        raise SystemExit(
            f"release engine binary not found at {engine_bin}; run scripts/build_release.sh"
        )
    proc = subprocess.Popen(  # noqa: S603
        [
            str(engine_bin),
            "--listen",
            "127.0.0.1:0",
            "--symbol",
            SYMBOL,
            "--venue",
            "kraken:fee_bps=0",
            "--venue",
            "coinbase:fee_bps=0",
            "--clock",
            "replay",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.stdout is None:
        proc.terminate()
        raise RuntimeError("engine subprocess has no stdout")
    line = proc.stdout.readline()
    match = re.search(r"port (\d+)", line)
    if match is None:
        proc.terminate()
        proc.wait(timeout=10)
        raise RuntimeError(f"engine did not start: {line!r}")
    return proc, f"127.0.0.1:{match.group(1)}"


async def _run_against(address: str) -> BenchResult:
    channel = EngineChannel(address)
    subscription: Subscription | None = None
    consumer: asyncio.Task[None] | None = None
    writer: MarketStreamWriter | None = None
    try:
        await channel.wait_ready(10.0)
        status = await channel.status()
        if status.clock_mode != pb.CLOCK_MODE_REPLAY:
            raise RuntimeError("engine is not running in replay mode; expected --clock replay")

        subscription = await Subscription.open(channel)
        send_times: dict[int, int] = {}
        latency_by_event: dict[int, int] = {}
        consumer = asyncio.ensure_future(
            _consume(subscription, ORDER_ID, send_times, latency_by_event)
        )

        writer = await MarketStreamWriter.open(channel, None)
        rng = random.Random(SEED)  # noqa: S311 -- deterministic synthetic data, not crypto
        snapshot_events = _snapshot_events(rng)
        for recv_ns, event in snapshot_events:
            await _send(writer, event, recv_ns, send_times)
        await _wait_processed(channel, len(snapshot_events))

        accepted, reason = await channel.submit(
            OrderSpec(ORDER_ID, "buy", ORDER_QTY, ORDER_DURATION_S, ORDER_SLICES, algo="pov"),
            start_ns=snapshot_events[-1][0],
            params=PovParams(PARTICIPATION),
        )
        if not accepted:
            raise RuntimeError(f"benchmark order rejected: {reason}")

        stream_events = _stream_events(rng, start_recv_ns=snapshot_events[-1][0])
        start_wall = time.perf_counter()
        for recv_ns, event in stream_events:
            await _send(writer, event, recv_ns, send_times)
        elapsed_s = time.perf_counter() - start_wall
        await writer.close()
        writer = None

        await _wait_processed(channel, len(snapshot_events) + len(stream_events))
        await _wait_for_fills(latency_by_event, NUM_TRADES)
        final_status = await channel.status()
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                await writer.close()
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        if subscription is not None:
            await subscription.close()
        await channel.close()

    samples = sorted(latency_by_event.values())
    events_per_s = len(stream_events) / elapsed_s if elapsed_s > 0 else float("inf")
    return BenchResult(
        total_events=len(stream_events),
        sample_count=len(samples),
        client_p50_us=_percentile_us(samples, 0.50),
        client_p99_us=_percentile_us(samples, 0.99),
        client_p999_us=_percentile_us(samples, 0.999),
        events_per_s=events_per_s,
        engine_p50_us=final_status.stats.latency_p50_ns / 1000.0,
        engine_p99_us=final_status.stats.latency_p99_ns / 1000.0,
    )


async def run_benchmark(engine_bin: Path) -> BenchResult:
    proc, address = _start_engine(engine_bin)
    try:
        return await _run_against(address)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def print_table(result: BenchResult) -> None:
    print(f"events sent:        {result.total_events}")
    print(f"fills sampled:      {result.sample_count}")
    print(f"events/s:           {result.events_per_s:,.0f}")
    print("client latency (send -> fill), microseconds:")
    print(f"  p50:   {result.client_p50_us:.1f}")
    print(f"  p99:   {result.client_p99_us:.1f}")
    print(f"  p99.9: {result.client_p999_us:.1f}")
    print("engine-side latency (ingest -> processed), microseconds:")
    print(f"  p50: {result.engine_p50_us:.1f}")
    print(f"  p99: {result.engine_p99_us:.1f}")


def _result_to_dict(result: BenchResult) -> dict[str, object]:
    return {
        "total_events": result.total_events,
        "sample_count": result.sample_count,
        "client_latency_us": {
            "p50": round(result.client_p50_us, 2),
            "p99": round(result.client_p99_us, 2),
            "p999": round(result.client_p999_us, 2),
        },
        "events_per_s": round(result.events_per_s, 1),
        "engine_latency_us": {
            "p50": round(result.engine_p50_us, 2),
            "p99": round(result.engine_p99_us, 2),
        },
    }


def write_baseline(path: Path, result: BenchResult) -> None:
    path.write_text(json.dumps(_result_to_dict(result), indent=2) + "\n")


def gate(result: BenchResult, baseline_path: Path, tolerance: float) -> int:
    if not baseline_path.exists():
        print(f"no baseline at {baseline_path}; skipping regression gate", file=sys.stderr)
        return 0
    baseline = json.loads(baseline_path.read_text())
    baseline_p50 = float(baseline["client_latency_us"]["p50"])
    limit = baseline_p50 * tolerance
    if result.client_p50_us > limit:
        print(
            f"REGRESSION: p50 {result.client_p50_us:.1f}us exceeds {tolerance:g}x baseline "
            f"{baseline_p50:.1f}us (limit {limit:.1f}us)",
            file=sys.stderr,
        )
        return 1
    print(f"OK: p50 {result.client_p50_us:.1f}us <= {tolerance:g}x baseline {baseline_p50:.1f}us")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-bin", type=Path, default=DEFAULT_ENGINE_BIN)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--tolerance", type=float, default=2.0)
    parser.add_argument("--write-baseline", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run_benchmark(args.engine_bin))
    print_table(result)
    if args.write_baseline:
        write_baseline(args.baseline, result)
        print(f"\nwrote baseline to {args.baseline}")
        return 0
    return gate(result, args.baseline, args.tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
