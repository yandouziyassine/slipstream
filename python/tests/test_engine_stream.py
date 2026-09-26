from __future__ import annotations

import asyncio
from typing import Any

import grpc
import grpc.aio
import pytest

from slipstream.engine_client import EngineError
from slipstream.engine_stream import (
    EngineBackpressureError,
    EngineChannel,
    MarketStreamWriter,
    Subscription,
    book_event,
    heartbeat_event,
    tick_event,
    trade_event,
)
from slipstream.models import BookUpdate, OrderSpec, TradeBatch
from slipstream.v1 import execution_pb2 as pb

_SEC = 1_000_000_000


def test_book_event_defaults_recv_ns_to_zero_for_live_mode() -> None:
    update = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "kraken")
    event = book_event(update, None)
    assert event.WhichOneof("event") == "book"
    assert event.book.recv_ns == 0
    assert event.book.venue == "kraken"


def test_book_event_carries_recv_ns_for_replay_mode() -> None:
    update = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "kraken")
    event = book_event(update, 123)
    assert event.book.recv_ns == 123


def test_trade_event_wraps_trade_batch() -> None:
    batch = TradeBatch("BTC/USD", False, ((100.0, 0.5),), "coinbase")
    event = trade_event(batch)
    assert event.WhichOneof("event") == "trades"
    assert event.trades.venue == "coinbase"


def test_heartbeat_event_is_empty() -> None:
    assert heartbeat_event("kraken").WhichOneof("event") == "heartbeat"
    assert heartbeat_event(None).WhichOneof("event") == "heartbeat"


def test_tick_event_carries_now_ns() -> None:
    event = tick_event(42)
    assert event.WhichOneof("event") == "tick"
    assert event.tick.now_ns == 42


class _FakeStreamUnaryCall:
    def __init__(self, error: Exception | None = None) -> None:
        self.written: list[pb.MarketEvent] = []
        self._error = error
        self._summary = pb.MarketStreamSummary(events=0)

    async def write(self, event: pb.MarketEvent) -> None:
        if self._error is not None:
            raise self._error
        self.written.append(event)
        self._summary = pb.MarketStreamSummary(events=len(self.written))

    async def done_writing(self) -> None:
        return None

    def __await__(self) -> Any:
        async def _result() -> pb.MarketStreamSummary:
            return self._summary

        return _result().__await__()


def test_send_nowait_raises_backpressure_error_when_queue_is_full() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall()
        writer = MarketStreamWriter(call, asyncio.Queue(maxsize=4))
        for _ in range(4):
            writer.send_nowait(pb.MarketEvent(heartbeat=pb.Heartbeat()))
        with pytest.raises(EngineBackpressureError):
            writer.send_nowait(pb.MarketEvent(heartbeat=pb.Heartbeat()))
        assert await writer.close() == 4

    asyncio.run(scenario())


def test_send_nowait_raises_stored_error_after_the_stream_dies() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall(
            error=grpc.aio.AioRpcError(
                grpc.StatusCode.INVALID_ARGUMENT, grpc.aio.Metadata(), grpc.aio.Metadata()
            )
        )
        writer = MarketStreamWriter(call, asyncio.Queue(maxsize=4))
        writer.send_nowait(pb.MarketEvent(heartbeat=pb.Heartbeat()))
        await asyncio.sleep(0)  # let the background pump observe the write failure
        with pytest.raises(EngineError, match="INVALID_ARGUMENT"):
            writer.send_nowait(pb.MarketEvent(heartbeat=pb.Heartbeat()))

    asyncio.run(scenario())


class _FakeUnaryStreamCall:
    def __init__(self, events: list[pb.EngineEvent]) -> None:
        self._events = list(events)
        self.cancelled = False

    async def initial_metadata(self) -> None:
        return None

    async def read(self) -> pb.EngineEvent | object:
        if not self._events:
            return grpc.aio.EOF
        return self._events.pop(0)

    def cancel(self) -> bool:
        self.cancelled = True
        return True


def test_subscription_raises_when_seq_does_not_increase() -> None:
    async def scenario() -> None:
        call = _FakeUnaryStreamCall(
            [
                pb.EngineEvent(seq=1, fill=pb.Fill(order_id="o")),
                pb.EngineEvent(seq=1, fill=pb.Fill(order_id="o")),
            ]
        )
        subscription = Subscription(call)
        first = await anext(subscription)
        assert first.seq == 1
        with pytest.raises(EngineError, match="seq"):
            await anext(subscription)

    asyncio.run(scenario())


def test_subscription_stops_iteration_at_eof() -> None:
    async def scenario() -> None:
        call = _FakeUnaryStreamCall([pb.EngineEvent(seq=1, fill=pb.Fill(order_id="o"))])
        subscription = Subscription(call)
        seen = [event async for event in subscription]
        assert [event.seq for event in seen] == [1]

    asyncio.run(scenario())


def test_subscription_close_cancels_the_call() -> None:
    async def scenario() -> None:
        call = _FakeUnaryStreamCall([])
        subscription = Subscription(call)
        await subscription.close()
        assert call.cancelled

    asyncio.run(scenario())


def test_replay_stream_book_and_trade_round_trip_visible_in_status(engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(engine_address)
        try:
            await channel.wait_ready(2.0)
            writer = await MarketStreamWriter.open(channel, None)
            book = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "kraken")
            writer.send_nowait(book_event(book, _SEC))
            trades = TradeBatch("BTC/USD", False, ((100.0, 0.5),), "kraken")
            writer.send_nowait(trade_event(trades))
            events = await writer.close()
            assert events == 2

            status = await channel.status()
            assert len(status.books) == 1
            assert status.books[0].venue == "kraken"
            assert [(level.price, level.qty) for level in status.books[0].bids] == [(99.0, 1.0)]
            assert status.clock_mode == pb.CLOCK_MODE_REPLAY
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_live_stream_rejects_nonzero_recv_ns(live_engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(live_engine_address)
        try:
            await channel.wait_ready(2.0)
            writer = await MarketStreamWriter.open(channel, "kraken")
            book = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "kraken")
            writer.send_nowait(book_event(book, 1))
            with pytest.raises(EngineError, match="INVALID_ARGUMENT"):
                await writer.close()
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_two_live_venue_streams_apply_concurrently(two_venue_live_engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(two_venue_live_engine_address)
        try:
            await channel.wait_ready(2.0)
            kraken_writer = await MarketStreamWriter.open(channel, "kraken")
            coinbase_writer = await MarketStreamWriter.open(channel, "coinbase")
            try:
                kraken_writer.send_nowait(
                    book_event(
                        BookUpdate("BTC/USD", True, ((10.0, 1.0),), ((11.0, 1.0),), "kraken"), None
                    )
                )
                coinbase_writer.send_nowait(
                    book_event(
                        BookUpdate("BTC/USD", True, ((20.0, 1.0),), ((21.0, 1.0),), "coinbase"),
                        None,
                    )
                )
            finally:
                kraken_events = await kraken_writer.close()
                coinbase_events = await coinbase_writer.close()
            assert (kraken_events, coinbase_events) == (1, 1)

            deadline = asyncio.get_event_loop().time() + 10.0
            venues: set[str] = set()
            while venues != {"kraken", "coinbase"} and asyncio.get_event_loop().time() < deadline:
                status = await channel.status()
                venues = {book.venue for book in status.books}
                if venues != {"kraken", "coinbase"}:
                    await asyncio.sleep(0.02)
            assert venues == {"kraken", "coinbase"}
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_duplicate_live_venue_stream_is_rejected(live_engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(live_engine_address)
        try:
            # The engine binds whichever stream it services first, so either of the two
            # concurrently opened writers may be the one that wins the venue; exactly one
            # of them must fail with FAILED_PRECONDITION.
            first = await MarketStreamWriter.open(channel, "kraken")
            second = await MarketStreamWriter.open(channel, "kraken")
            errors: list[EngineError | None] = []
            for writer in (first, second):
                try:
                    await writer.close()
                    errors.append(None)
                except EngineError as exc:
                    errors.append(exc)
            rejected = [exc for exc in errors if exc is not None]
            assert len(rejected) == 1
            assert "FAILED_PRECONDITION" in str(rejected[0])
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_subscribe_then_submit_yields_fills_and_a_terminal_update(engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(engine_address)
        try:
            await channel.wait_ready(2.0)
            subscription = await Subscription.open(channel)
            try:
                writer = await MarketStreamWriter.open(channel, None)
                book = BookUpdate("BTC/USD", True, ((99.98, 100.0),), ((100.02, 100.0),), "kraken")
                writer.send_nowait(book_event(book, _SEC))
                assert await writer.close() == 1

                accepted, reason = await channel.submit(
                    OrderSpec("o-1", "buy", 0.2, 1, 2), start_ns=_SEC
                )
                assert accepted, reason

                first = await anext(subscription)
                assert first.seq == 1
                assert first.WhichOneof("event") == "fill"
                assert first.fill.order_id == "o-1"
                assert first.fill.qty == pytest.approx(0.1)

                tick_writer = await MarketStreamWriter.open(channel, None)
                tick_writer.send_nowait(tick_event(2 * _SEC))
                assert await tick_writer.close() == 1

                filled = first.fill.qty
                last_seq = first.seq
                terminal: pb.OrderUpdate | None = None
                while terminal is None:
                    event = await anext(subscription)
                    assert event.seq > last_seq
                    last_seq = event.seq
                    if event.WhichOneof("event") == "fill":
                        filled += event.fill.qty
                    elif event.order.state != pb.ORDER_STATE_WORKING:
                        terminal = event.order

                assert terminal.order_id == "o-1"
                assert terminal.state == pb.ORDER_STATE_COMPLETED
                assert terminal.filled_qty == pytest.approx(0.2)
                assert filled == pytest.approx(0.2)
            finally:
                await subscription.close()
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_second_subscription_is_rejected_while_the_first_is_open(engine_address: str) -> None:
    async def scenario() -> None:
        channel = EngineChannel(engine_address)
        try:
            first = await Subscription.open(channel)
            try:
                # open() only waits for initial metadata; the engine sends that unconditionally,
                # so the rejection surfaces once we try to read from the second subscription.
                second = await Subscription.open(channel)
                with pytest.raises(EngineError, match="FAILED_PRECONDITION"):
                    await anext(second)
            finally:
                await first.close()
        finally:
            await channel.close()

    asyncio.run(scenario())
