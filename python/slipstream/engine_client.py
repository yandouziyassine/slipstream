from __future__ import annotations

import math
from typing import Any, cast

import grpc

from slipstream.config import validate_engine_address
from slipstream.models import (
    VENUES,
    AlmgrenChrissParams,
    BookUpdate,
    Fill,
    OrderSpec,
    PovParams,
    ScheduleParams,
    StepResult,
    TradeBatch,
    Venue,
    VwapParams,
)
from slipstream.v1 import execution_pb2 as pb
from slipstream.v1 import execution_pb2_grpc as pb_grpc

_NS_PER_S = 1_000_000_000
_SIDES = {"buy": pb.SIDE_BUY, "sell": pb.SIDE_SELL}


class EngineError(RuntimeError):
    pass


def book_update_to_proto(update: BookUpdate, recv_ns: int) -> pb.BookUpdate:
    return pb.BookUpdate(
        symbol=update.symbol,
        is_snapshot=update.is_snapshot,
        bids=[pb.PriceLevel(price=price, qty=qty) for price, qty in update.bids],
        asks=[pb.PriceLevel(price=price, qty=qty) for price, qty in update.asks],
        venue=update.venue,
        recv_ns=recv_ns,
    )


def order_to_proto(
    spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
) -> pb.ParentOrder:
    order = pb.ParentOrder(
        order_id=spec.order_id,
        side=_SIDES[spec.side],
        qty=spec.qty,
        start_ns=start_ns,
        duration_ns=spec.duration_s * _NS_PER_S,
        num_slices=spec.num_slices,
    )
    if isinstance(params, VwapParams):
        order.vwap.weights.extend(params.weights)
    elif isinstance(params, AlmgrenChrissParams):
        order.almgren_chriss.sigma = params.sigma
        order.almgren_chriss.eta = params.eta
        order.almgren_chriss.risk_aversion = params.risk_aversion
    elif isinstance(params, PovParams):
        order.pov.participation = params.participation
    else:
        order.twap.SetInParent()
    return order


def trade_batch_to_proto(batch: TradeBatch) -> pb.TradeBatch:
    return pb.TradeBatch(
        symbol=batch.symbol,
        trades=[pb.Trade(price=price, qty=qty) for price, qty in batch.trades],
        venue=batch.venue,
    )


class EngineClient:
    def __init__(self, address: str, timeout_s: float = 2.0) -> None:
        validate_engine_address(address)
        self._timeout_s = timeout_s
        self._channel = grpc.insecure_channel(address)
        self._stub = pb_grpc.ExecutionEngineStub(self._channel)

    def wait_ready(self, timeout_s: float = 10.0) -> None:
        try:
            grpc.channel_ready_future(self._channel).result(timeout=timeout_s)
        except grpc.FutureTimeoutError as exc:
            raise EngineError("engine not reachable") from exc

    def apply_book(self, update: BookUpdate, recv_ns: int) -> None:
        self._call(self._stub.ApplyBookUpdate, book_update_to_proto(update, recv_ns))

    def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]:
        reply = cast(
            pb.SubmitReply,
            self._call(self._stub.SubmitParentOrder, order_to_proto(spec, start_ns, params)),
        )
        return reply.accepted, reply.reason

    def apply_trades(self, batch: TradeBatch) -> None:
        self._call(self._stub.ApplyTrades, trade_batch_to_proto(batch))

    def step(self, now_ns: int) -> StepResult:
        reply = cast(pb.StepReply, self._call(self._stub.Step, pb.StepRequest(now_ns=now_ns)))
        fills = [Fill(f.order_id, f.ts_ns, f.qty, f.price, f.venue, f.fee) for f in reply.fills]
        return StepResult(fills=fills, working_orders=reply.working_orders)

    def status(self) -> pb.StatusReply:
        return cast(pb.StatusReply, self._call(self._stub.GetStatus, pb.StatusRequest()))

    def book_depth(self) -> int:
        return self.status().book_depth

    def venue_fees(self) -> dict[Venue, float]:
        fees: dict[Venue, float] = {}
        for info in self.status().venues:
            if info.name not in VENUES or not math.isfinite(info.fee_bps) or info.fee_bps < 0:
                raise EngineError(f"engine reported an invalid venue {info.name[:32]!r}")
            fees[info.name] = info.fee_bps
        if not fees:
            raise EngineError("engine reported no venues")
        return fees

    def close(self) -> None:
        self._channel.close()

    def _call(self, method: Any, request: Any) -> Any:
        try:
            return method(request, timeout=self._timeout_s)
        except grpc.RpcError as exc:
            raise EngineError(f"engine call failed: {exc.code().name}: {exc.details()}") from exc
