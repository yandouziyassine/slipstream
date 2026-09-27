from __future__ import annotations

import asyncio
import logging
import math
from types import TracebackType
from typing import Any, cast

import grpc
import grpc.aio

from slipstream.config import validate_engine_address
from slipstream.engine_client import (
    EngineError,
    book_update_to_proto,
    order_to_proto,
    trade_batch_to_proto,
)
from slipstream.models import VENUES, BookUpdate, OrderSpec, ScheduleParams, TradeBatch, Venue
from slipstream.v1 import execution_pb2 as pb
from slipstream.v1 import execution_pb2_grpc as pb_grpc

_log = logging.getLogger(__name__)

_MARKET_QUEUE_CAPACITY = 1000
_VENUE_METADATA_KEY = "slipstream-venue"
_SUBSCRIBED_METADATA_KEY = "slipstream-subscribed"


class EngineBackpressureError(EngineError):
    pass


def _rpc_error(action: str, exc: grpc.aio.AioRpcError) -> EngineError:
    return EngineError(f"{action} failed: {exc.code().name}: {exc.details()}")


def book_event(update: BookUpdate, recv_ns: int | None) -> pb.MarketEvent:
    return pb.MarketEvent(book=book_update_to_proto(update, 0 if recv_ns is None else recv_ns))


def trade_event(batch: TradeBatch) -> pb.MarketEvent:
    return pb.MarketEvent(trades=trade_batch_to_proto(batch))


def heartbeat_event(venue: Venue | None) -> pb.MarketEvent:
    # Heartbeat carries no fields: live mode binds it to the stream's own venue, and replay
    # mode accepts it only for a single-venue engine. venue is accepted for API symmetry with
    # the other converters and to make the caller's intent explicit at the call site.
    _ = venue
    return pb.MarketEvent(heartbeat=pb.Heartbeat())


def tick_event(now_ns: int) -> pb.MarketEvent:
    return pb.MarketEvent(tick=pb.Tick(now_ns=now_ns))


class EngineChannel:
    def __init__(self, address: str, timeout_s: float = 2.0) -> None:
        validate_engine_address(address)
        self._timeout_s = timeout_s
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = pb_grpc.ExecutionEngineStub(self._channel)

    @property
    def stub(self) -> pb_grpc.ExecutionEngineStub:
        return self._stub

    async def wait_ready(self, timeout_s: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._channel.channel_ready(), timeout=timeout_s)
        except (TimeoutError, grpc.aio.AioRpcError) as exc:
            raise EngineError("engine not reachable") from exc

    async def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]:
        reply = cast(
            pb.SubmitReply,
            await self._call(self._stub.SubmitParentOrder, order_to_proto(spec, start_ns, params)),
        )
        return reply.accepted, reply.reason

    async def status(self) -> pb.StatusReply:
        return cast(pb.StatusReply, await self._call(self._stub.GetStatus, pb.StatusRequest()))

    async def book_depth(self) -> int:
        return (await self.status()).book_depth

    async def venue_fees(self) -> dict[Venue, float]:
        fees: dict[Venue, float] = {}
        for info in (await self.status()).venues:
            if info.name not in VENUES or not math.isfinite(info.fee_bps) or info.fee_bps < 0:
                raise EngineError(f"engine reported an invalid venue {info.name[:32]!r}")
            fees[info.name] = info.fee_bps
        if not fees:
            raise EngineError("engine reported no venues")
        return fees

    async def close(self) -> None:
        await self._channel.close()

    async def _call(self, method: Any, request: Any) -> Any:
        try:
            return await method(request, timeout=self._timeout_s)
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error("engine call", exc) from exc


class MarketStreamWriter:
    def __init__(self, call: Any, queue: asyncio.Queue[pb.MarketEvent | None]) -> None:
        self._call = call
        self._queue = queue
        self._error: EngineError | None = None
        self._closed = False
        self._task = asyncio.ensure_future(self._pump())

    @classmethod
    async def open(cls, channel: EngineChannel, venue: Venue | None) -> MarketStreamWriter:
        metadata = ((_VENUE_METADATA_KEY, venue),) if venue is not None else None
        call = channel.stub.MarketStream(metadata=metadata)
        queue: asyncio.Queue[pb.MarketEvent | None] = asyncio.Queue(maxsize=_MARKET_QUEUE_CAPACITY)
        return cls(call, queue)

    async def __aenter__(self) -> MarketStreamWriter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._closed:
            return
        if exc is None:
            await self.close()
        elif not isinstance(exc, Exception):
            # Cancellation or interpreter exit: stop now rather than wait to flush the queue.
            self._closed = True
            await self._abort()
        else:
            try:
                await self.close()
            except Exception as close_error:
                _log.warning(
                    "closing the market stream after an error also failed: %s", close_error
                )
                exc.add_note(f"closing the market stream also failed: {close_error}")

    def send_nowait(self, event: pb.MarketEvent) -> None:
        if self._closed:
            raise EngineError("market stream is closed")
        if self._error is not None:
            raise self._error
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise EngineBackpressureError("market stream queue is full") from exc

    async def close(self) -> int:
        if self._closed:
            raise EngineError("market stream is closed")
        self._closed = True
        try:
            await self._queue.put(None)
            await self._task
            if self._error is not None:
                raise self._error
            try:
                await self._call.done_writing()
                summary = await self._call
            except grpc.aio.AioRpcError as exc:
                raise _rpc_error("market stream", exc) from exc
        except BaseException:
            await self._abort()
            raise
        return cast(int, summary.events)

    async def _abort(self) -> None:
        self._task.cancel()
        self._call.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _pump(self) -> None:
        error = await self._drain_until_closed_or_error()
        if error is not None:
            self._error = error
            await self._discard_until_closed()

    async def _drain_until_closed_or_error(self) -> EngineError | None:
        while True:
            event = await self._queue.get()
            if event is None:
                return None
            try:
                await self._call.write(event)
            except grpc.aio.AioRpcError as exc:
                return _rpc_error("market stream", exc)

    async def _discard_until_closed(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return


def _is_subscribed(metadata: Any) -> bool:
    if metadata is None:
        return False
    return any(key == _SUBSCRIBED_METADATA_KEY and value == "1" for key, value in metadata)


class Subscription:
    def __init__(self, call: Any) -> None:
        self._call = call
        self._last_seq: int | None = None

    @classmethod
    async def open(cls, channel: EngineChannel) -> Subscription:
        return await cls.from_call(channel.stub.Subscribe(pb.SubscribeRequest()))

    @classmethod
    async def from_call(cls, call: Any) -> Subscription:
        try:
            metadata = await call.initial_metadata()
            if _is_subscribed(metadata):
                return cls(call)
            # A rejected call still resolves its (empty) initial metadata: report its status.
            code = await call.code()
            details = await call.details()
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error("subscribe", exc) from exc
        call.cancel()
        raise EngineError(f"subscribe failed: {code.name}: {details}")

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> pb.EngineEvent:
        try:
            event = await self._call.read()
        except grpc.aio.AioRpcError as exc:
            raise _rpc_error("subscribe", exc) from exc
        if event is grpc.aio.EOF:
            raise StopAsyncIteration
        event = cast(pb.EngineEvent, event)
        if self._last_seq is not None and event.seq <= self._last_seq:
            raise EngineError(
                f"subscription seq {event.seq} did not increase past {self._last_seq}"
            )
        self._last_seq = event.seq
        return event

    async def close(self) -> None:
        self._call.cancel()
