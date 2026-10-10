from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from hypothesis import settings
from pg_cluster import pg_cluster, pg_server  # noqa: F401 - registers the Postgres fixtures

from slipstream.calibration import CalibrationData
from slipstream.engine_stream import EngineChannel
from slipstream.models import OrderSpec, Venue
from slipstream.replay import read_replay
from slipstream.session import SessionResult, check_clock_mode, run_replay_session
from slipstream.v1 import execution_pb2 as pb

# "ci" reproduces the same 200 examples on every run so PRs stay deterministic and fast.
# "nightly" trades speed for coverage: a random seed explores cases "ci" never will, and
# print_blob makes any failure reproducible with an @example / --hypothesis-seed pin.
settings.register_profile("ci", derandomize=True, max_examples=200, deadline=None, database=None)
settings.register_profile(
    "nightly", derandomize=False, max_examples=2000, deadline=None, print_blob=True
)
settings.load_profile(os.environ.get("SLIPSTREAM_HYPOTHESIS_PROFILE", "ci"))

ENGINE_BIN = Path(__file__).resolve().parents[2] / "build" / "engine" / "slipstream_engine"


def _run_engine(*extra: str) -> Iterator[str]:
    if not ENGINE_BIN.exists():
        pytest.fail(f"engine binary not found at {ENGINE_BIN}; run scripts/build_engine.sh")
    proc = subprocess.Popen(
        [
            str(ENGINE_BIN),
            "--listen",
            "127.0.0.1:0",
            "--symbol",
            "BTC/USD",
            "--max-order-notional",
            "10000",
            "--max-position",
            "1.0",
            "--clock",
            "replay",
            *extra,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        match = re.search(r"port (\d+)", line)
        if match is None:
            pytest.fail(f"engine did not start: {line!r}")
        yield f"127.0.0.1:{match.group(1)}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def engine_address() -> Iterator[str]:
    yield from _run_engine()


@pytest.fixture
def two_venue_engine_address() -> Iterator[str]:
    yield from _run_engine("--venue", "kraken:fee_bps=0", "--venue", "coinbase:fee_bps=1")


@pytest.fixture
def live_engine_address() -> Iterator[str]:
    yield from _run_engine("--clock", "live")


@pytest.fixture
def two_venue_live_engine_address() -> Iterator[str]:
    yield from _run_engine(
        "--venue", "kraken:fee_bps=0", "--venue", "coinbase:fee_bps=1", "--clock", "live"
    )


@pytest.fixture
def kraken_min_qty_engine_address() -> Iterator[str]:
    yield from _run_engine("--venue", "kraken:fee_bps=0,min_qty=0.00005")


def run_session_on_file(
    address: str,
    specs: Sequence[OrderSpec],
    file: Path,
    venues: Sequence[Venue] = ("kraken",),
    calibration: CalibrationData | None = None,
) -> SessionResult:
    """One replay session against a real engine, the way `slipstream replay` runs it."""

    async def scenario() -> SessionResult:
        channel = EngineChannel(address)
        try:
            await channel.wait_ready(5.0)
            await check_clock_mode(channel, pb.CLOCK_MODE_REPLAY)
            return await run_replay_session(
                channel,
                specs,
                "BTC/USD",
                logging.getLogger("test"),
                records=read_replay(file),
                calibration=calibration,
                venues=venues,
                fee_bps=await channel.venue_fees(),
            )
        finally:
            await channel.close()

    return asyncio.run(scenario())


@pytest.fixture
def replay_session() -> Callable[..., SessionResult]:
    return run_session_on_file
