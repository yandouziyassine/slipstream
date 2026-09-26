from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from slipstream.models import BookUpdate, Fill, OrderSpec, ScheduleParams, StepResult, TradeBatch
from slipstream.v1 import execution_pb2 as pb

ENGINE_BIN = Path(__file__).resolve().parents[2] / "build" / "engine" / "slipstream_engine"


class FakeEngine:
    def __init__(self) -> None:
        self.books: list[BookUpdate] = []
        self.book_recv_ns: list[int] = []
        self.submits: list[tuple[OrderSpec, int, ScheduleParams | None]] = []
        self.steps: list[int] = []
        self.accept = True
        self.reason = ""
        self.state: int = pb.ORDER_STATE_WORKING
        self.done_after_steps: int | None = None
        self.fills_per_step: list[list[Fill]] = []
        self.trades: list[TradeBatch] = []
        self.reject_ids: set[str] = set()

    def apply_book(self, update: BookUpdate, recv_ns: int) -> None:
        self.books.append(update)
        self.book_recv_ns.append(recv_ns)

    def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]:
        self.submits.append((spec, start_ns, params))
        if spec.order_id in self.reject_ids:
            return False, "position limit exceeded"
        return self.accept, self.reason

    def apply_trades(self, batch: TradeBatch) -> None:
        self.trades.append(batch)

    def step(self, now_ns: int) -> StepResult:
        self.steps.append(now_ns)
        fills = self.fills_per_step.pop(0) if self.fills_per_step else []
        return StepResult(fills=fills, working_orders=self._working_orders())

    def _working_orders(self) -> int:
        total = len(self.submits)
        if total == 0:
            return 0
        if self.done_after_steps is not None and len(self.steps) >= self.done_after_steps:
            return 0
        if self.state in (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED):
            return 0
        return total

    def status(self) -> pb.StatusReply:
        if not self.accept:
            return pb.StatusReply()
        state = self.state
        if self.done_after_steps is not None and len(self.steps) >= self.done_after_steps:
            state = pb.ORDER_STATE_COMPLETED
        orders = [
            pb.OrderStatus(order_id=spec.order_id, state=state, algo=spec.algo)
            for spec, _, _ in self.submits
        ]
        return pb.StatusReply(orders=orders)


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()


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
def kraken_min_qty_engine_address() -> Iterator[str]:
    yield from _run_engine("--venue", "kraken:fee_bps=0,min_qty=0.00005")
