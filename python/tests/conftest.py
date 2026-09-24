from __future__ import annotations

import pytest

from slipstream.models import BookUpdate, Fill, OrderSpec
from slipstream.v1 import execution_pb2 as pb


class FakeEngine:
    def __init__(self) -> None:
        self.books: list[BookUpdate] = []
        self.submits: list[tuple[OrderSpec, int]] = []
        self.steps: list[int] = []
        self.accept = True
        self.reason = ""
        self.state: int = pb.ORDER_STATE_WORKING
        self.done_after_steps: int | None = None
        self.fills_per_step: list[list[Fill]] = []

    def apply_book(self, update: BookUpdate) -> None:
        self.books.append(update)

    def submit(self, spec: OrderSpec, start_ns: int) -> tuple[bool, str]:
        self.submits.append((spec, start_ns))
        return self.accept, self.reason

    def step(self, now_ns: int) -> list[Fill]:
        self.steps.append(now_ns)
        return self.fills_per_step.pop(0) if self.fills_per_step else []

    def status(self) -> pb.StatusReply:
        if not self.submits or not self.accept:
            return pb.StatusReply()
        state = self.state
        if self.done_after_steps is not None and len(self.steps) >= self.done_after_steps:
            state = pb.ORDER_STATE_COMPLETED
        order = pb.OrderStatus(order_id=self.submits[0][0].order_id, state=state)
        return pb.StatusReply(orders=[order])


@pytest.fixture
def fake_engine() -> FakeEngine:
    return FakeEngine()
