from __future__ import annotations

import logging
from typing import Protocol

from slipstream.kraken import KrakenMessageError, parse_message
from slipstream.models import BookUpdate, Fill, OrderSpec
from slipstream.v1 import execution_pb2 as pb

_TERMINAL_STATES = (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED)


class Engine(Protocol):
    def apply_book(self, update: BookUpdate) -> None: ...
    def submit(self, spec: OrderSpec, start_ns: int) -> tuple[bool, str]: ...
    def step(self, now_ns: int) -> list[Fill]: ...
    def status(self) -> pb.StatusReply: ...


class OrderRejectedError(RuntimeError):
    pass


class ExecutionRunner:
    def __init__(
        self, engine: Engine, spec: OrderSpec, symbol: str, logger: logging.Logger
    ) -> None:
        self._engine = engine
        self._spec = spec
        self._symbol = symbol
        self._log = logger
        self._submitted = False
        self.fills: list[Fill] = []

    def on_message(self, raw: str | bytes, now_ns: int) -> None:
        update = parse_message(raw)
        if isinstance(update, BookUpdate):
            if update.symbol != self._symbol:
                raise KrakenMessageError(f"unexpected symbol {update.symbol!r}")
            self._engine.apply_book(update)
            if update.is_snapshot and not self._submitted:
                self._submit(now_ns)
        if self._submitted:
            self._step(now_ns)

    def order_status(self) -> pb.OrderStatus | None:
        for order in self._engine.status().orders:
            if order.order_id == self._spec.order_id:
                return order
        return None

    def is_done(self) -> bool:
        status = self.order_status()
        return status is not None and status.state in _TERMINAL_STATES

    def _submit(self, now_ns: int) -> None:
        accepted, reason = self._engine.submit(self._spec, now_ns)
        if not accepted:
            raise OrderRejectedError(f"order rejected: {reason}")
        self._submitted = True
        self._log.info(
            "order submitted",
            extra={
                "fields": {
                    "event": "submit",
                    "order_id": self._spec.order_id,
                    "side": self._spec.side,
                    "qty": self._spec.qty,
                    "duration_s": self._spec.duration_s,
                    "slices": self._spec.num_slices,
                }
            },
        )

    def _step(self, now_ns: int) -> None:
        for fill in self._engine.step(now_ns):
            self.fills.append(fill)
            self._log.info(
                "fill",
                extra={
                    "fields": {
                        "event": "fill",
                        "order_id": fill.order_id,
                        "qty": fill.qty,
                        "price": fill.price,
                        "ts_ns": fill.ts_ns,
                    }
                },
            )
