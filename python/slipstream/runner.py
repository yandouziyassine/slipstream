from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from slipstream.calibration import CalibrationData, schedule_params
from slipstream.coinbase import CoinbaseStream
from slipstream.kraken import KrakenMessageError, parse_message
from slipstream.models import BookUpdate, Fill, OrderSpec, ScheduleParams, TradeBatch, Venue
from slipstream.v1 import execution_pb2 as pb

_TERMINAL_STATES = (pb.ORDER_STATE_COMPLETED, pb.ORDER_STATE_HALTED)
_VALID_VENUES: frozenset[Venue] = frozenset({"kraken", "coinbase"})

_Parser = Callable[[str | bytes], BookUpdate | TradeBatch | None]


class Engine(Protocol):
    def apply_book(self, update: BookUpdate) -> None: ...
    def apply_trades(self, batch: TradeBatch) -> None: ...
    def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]: ...
    def step(self, now_ns: int) -> list[Fill]: ...
    def status(self) -> pb.StatusReply: ...


class OrderRejectedError(RuntimeError):
    pass


class ExecutionRunner:
    def __init__(
        self,
        engine: Engine,
        specs: OrderSpec | Sequence[OrderSpec],
        symbol: str,
        logger: logging.Logger,
        calibration: CalibrationData | None = None,
        venues: Sequence[Venue] = ("kraken",),
        book_depth: int = 10,
    ) -> None:
        if not venues:
            raise ValueError("venues must not be empty")
        if len(set(venues)) != len(venues):
            raise ValueError(f"duplicate venue in {venues!r}")
        unknown = [venue for venue in venues if venue not in _VALID_VENUES]
        if unknown:
            raise ValueError(f"unknown venue(s) {unknown!r}")
        self._engine = engine
        self._specs = (specs,) if isinstance(specs, OrderSpec) else tuple(specs)
        self._symbol = symbol
        self._log = logger
        self._calibration = calibration
        self._venues: frozenset[Venue] = frozenset(venues)
        self._parsers: dict[Venue, _Parser] = {}
        if "kraken" in self._venues:
            self._parsers["kraken"] = parse_message
        if "coinbase" in self._venues:
            self._parsers["coinbase"] = CoinbaseStream(symbol, book_depth).parse
        self._snapshot_venues: set[Venue] = set()
        self._submitted = False
        self.fills: list[Fill] = []

    def on_message(self, raw: str | bytes, now_ns: int, venue: Venue = "kraken") -> None:
        parser = self._parsers.get(venue)
        if parser is None:
            raise ValueError(f"unconfigured venue {venue!r}")
        update = parser(raw)
        if isinstance(update, BookUpdate):
            self._check_symbol(update.symbol)
            self._engine.apply_book(update)
            if update.is_snapshot and not self._submitted:
                self._snapshot_venues.add(update.venue)
                if self._snapshot_venues >= self._venues:
                    self._submit_all(update, now_ns)
        elif isinstance(update, TradeBatch):
            self._check_symbol(update.symbol)
            if not update.is_snapshot:
                self._engine.apply_trades(update)
        if self._submitted:
            self._step(now_ns)

    def order_statuses(self) -> list[pb.OrderStatus]:
        by_id = {order.order_id: order for order in self._engine.status().orders}
        return [by_id[spec.order_id] for spec in self._specs if spec.order_id in by_id]

    def order_status(self) -> pb.OrderStatus | None:
        statuses = self.order_statuses()
        return statuses[0] if statuses else None

    def is_done(self) -> bool:
        statuses = self.order_statuses()
        return len(statuses) == len(self._specs) and all(
            status.state in _TERMINAL_STATES for status in statuses
        )

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self._symbol:
            raise KrakenMessageError(f"unexpected symbol {symbol!r}")

    def _submit_all(self, book: BookUpdate, now_ns: int) -> None:
        planned = [
            (spec, schedule_params(spec, book, now_ns, self._calibration)) for spec in self._specs
        ]
        for spec, params in planned:
            accepted, reason = self._engine.submit(spec, now_ns, params)
            if not accepted:
                raise OrderRejectedError(f"order {spec.order_id} rejected: {reason}")
            self._log.info(
                "order submitted",
                extra={
                    "fields": {
                        "event": "submit",
                        "order_id": spec.order_id,
                        "algo": spec.algo,
                        "side": spec.side,
                        "qty": spec.qty,
                        "duration_s": spec.duration_s,
                        "slices": spec.num_slices,
                        "params": repr(params),
                    }
                },
            )
        self._submitted = True

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
