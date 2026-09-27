from __future__ import annotations

import asyncio
from typing import Any

import grpc
import grpc.aio
import pytest

from slipstream.config import ConfigError
from slipstream.engine_stream import (
    EngineBackpressureError,
    EngineChannel,
    EngineError,
    MarketStreamWriter,
    Subscription,
    book_event,
    book_update_to_proto,
    heartbeat_event,
    order_to_proto,
    tick_event,
    trade_batch_to_proto,
    trade_event,
)
from slipstream.models import (
    AlmgrenChrissParams,
    BookUpdate,
    Fill,
    OrderSpec,
    PovParams,
    TradeBatch,
    TwapParams,
    VwapParams,
)
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
    event = trade_event(batch, None)
    assert event.WhichOneof("event") == "trades"
    assert event.trades.venue == "coinbase"
    assert event.trades.recv_ns == 0
    assert trade_event(batch, 123).trades.recv_ns == 123


def test_heartbeat_event_names_its_venue_and_time() -> None:
    live = heartbeat_event("kraken", None)
    assert live.WhichOneof("event") == "heartbeat"
    assert (live.heartbeat.venue, live.heartbeat.recv_ns) == ("kraken", 0)
    replay = heartbeat_event("coinbase", 123)
    assert (replay.heartbeat.venue, replay.heartbeat.recv_ns) == ("coinbase", 123)


def test_tick_event_carries_now_ns() -> None:
    event = tick_event(42)
    assert event.WhichOneof("event") == "tick"
    assert event.tick.now_ns == 42


def test_book_update_to_proto() -> None:
    update = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "coinbase")
    msg = book_update_to_proto(update, recv_ns=123)
    assert msg.symbol == "BTC/USD"
    assert msg.is_snapshot
    assert msg.venue == "coinbase"
    assert msg.recv_ns == 123
    assert [(level.price, level.qty) for level in msg.bids] == [(99.0, 1.0)]
    assert [(level.price, level.qty) for level in msg.asks] == [(101.0, 2.0)]


def test_order_to_proto_converts_seconds_to_nanoseconds() -> None:
    msg = order_to_proto(OrderSpec("o-1", "sell", 0.5, 60, 6), start_ns=123)
    assert msg.order_id == "o-1"
    assert msg.side == pb.SIDE_SELL
    assert msg.start_ns == 123
    assert msg.duration_ns == 60_000_000_000
    assert msg.num_slices == 6


def test_order_defaults_to_twap_schedule() -> None:
    msg = order_to_proto(OrderSpec("o-1", "buy", 1.0, 4, 4), start_ns=0)
    assert msg.WhichOneof("schedule") == "twap"


def test_order_carries_schedule_params() -> None:
    spec = OrderSpec("o-1", "buy", 1.0, 4, 2)
    vwap = order_to_proto(spec, 0, VwapParams((1.0, 3.0)))
    assert vwap.WhichOneof("schedule") == "vwap"
    assert list(vwap.vwap.weights) == [1.0, 3.0]
    ac = order_to_proto(spec, 0, AlmgrenChrissParams(0.5, 2.0, 3e-5))
    assert ac.WhichOneof("schedule") == "almgren_chriss"
    assert (ac.almgren_chriss.sigma, ac.almgren_chriss.eta, ac.almgren_chriss.risk_aversion) == (
        0.5,
        2.0,
        3e-5,
    )
    pov = order_to_proto(spec, 0, PovParams(0.25))
    assert pov.WhichOneof("schedule") == "pov"
    assert pov.pov.participation == 0.25
    assert order_to_proto(spec, 0, TwapParams()).WhichOneof("schedule") == "twap"


def test_trade_batch_to_proto_carries_trades_venue_and_time() -> None:
    msg = trade_batch_to_proto(
        TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3)), "coinbase"), recv_ns=7
    )
    assert msg.symbol == "BTC/USD"
    assert msg.venue == "coinbase"
    assert msg.recv_ns == 7
    assert [(t.price, t.qty) for t in msg.trades] == [(100.5, 0.2), (100.4, 0.3)]


def test_fill_defaults_keep_single_venue_callers_working() -> None:
    fill = Fill("o", 1, 0.5, 100.0)
    assert (fill.venue, fill.fee) == ("kraken", 0.0)


def test_channel_rejects_non_loopback_address() -> None:
    with pytest.raises(ConfigError):
        EngineChannel("10.0.0.1:50051")


def _with_channel(body: Any, timeout_s: float = 2.0) -> Any:
    async def scenario() -> Any:
        channel = EngineChannel("127.0.0.1:1", timeout_s=timeout_s)
        try:
            return await body(channel)
        finally:
            await channel.close()

    return asyncio.run(scenario())


def test_wait_ready_times_out_when_engine_absent() -> None:
    async def body(channel: EngineChannel) -> None:
        await channel.wait_ready(timeout_s=0.5)

    with pytest.raises(EngineError, match="not reachable"):
        _with_channel(body)


def test_unreachable_engine_raises_engine_error() -> None:
    async def body(channel: EngineChannel) -> None:
        await channel.status()

    with pytest.raises(EngineError, match="engine call failed"):
        _with_channel(body, timeout_s=0.5)


def _fake_status(*venues: tuple[str, float]) -> pb.StatusReply:
    return pb.StatusReply(
        venues=[pb.VenueInfo(name=n, fee_bps=f) for n, f in venues], book_depth=25
    )


def test_venue_fees_and_book_depth_read_the_engine_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(channel: EngineChannel) -> tuple[dict[str, float], int]:
        async def status() -> pb.StatusReply:
            return _fake_status(("kraken", 40.0), ("coinbase", 60.0))

        monkeypatch.setattr(channel, "status", status)
        return dict(await channel.venue_fees()), await channel.book_depth()

    assert _with_channel(body) == ({"kraken": 40.0, "coinbase": 60.0}, 25)


@pytest.mark.parametrize(
    "venues",
    [(), (("binance", 10.0),), (("kraken", float("nan")),), (("kraken", -1.0),)],
    ids=["none", "unknown", "nan", "negative"],
)
def test_venue_fees_rejects_bad_engine_venues(
    monkeypatch: pytest.MonkeyPatch, venues: tuple[tuple[str, float], ...]
) -> None:
    async def body(channel: EngineChannel) -> None:
        async def status() -> pb.StatusReply:
            return _fake_status(*venues)

        monkeypatch.setattr(channel, "status", status)
        await channel.venue_fees()

    with pytest.raises(EngineError):
        _with_channel(body)


class _FakeStreamUnaryCall:
    def __init__(self, error: Exception | None = None, block_writes: bool = False) -> None:
        self.written: list[pb.MarketEvent] = []
        self._error = error
        self._block_writes = block_writes
        self._summary = pb.MarketStreamSummary(events=0)
        self.done_writing_called = False
        self.cancelled = False

    async def write(self, event: pb.MarketEvent) -> None:
        if self._block_writes:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        self.written.append(event)
        self._summary = pb.MarketStreamSummary(events=len(self.written))

    async def done_writing(self) -> None:
        self.done_writing_called = True

    def cancel(self) -> bool:
        self.cancelled = True
        return True

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


def _heartbeat() -> pb.MarketEvent:
    return pb.MarketEvent(heartbeat=pb.Heartbeat())


def _invalid_argument() -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        grpc.StatusCode.INVALID_ARGUMENT, grpc.aio.Metadata(), grpc.aio.Metadata()
    )


def _other_tasks() -> set[asyncio.Task[Any]]:
    return asyncio.all_tasks() - {asyncio.current_task()}


def test_writer_context_manager_closes_the_stream_on_normal_exit() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall()
        async with MarketStreamWriter(call, asyncio.Queue(maxsize=4)) as writer:
            writer.send_nowait(_heartbeat())
            writer.send_nowait(_heartbeat())
        assert len(call.written) == 2
        assert call.done_writing_called
        assert not _other_tasks()

    asyncio.run(scenario())


def test_writer_context_manager_closes_and_reraises_on_exception_exit() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall()
        with pytest.raises(ValueError, match="boom"):
            async with MarketStreamWriter(call, asyncio.Queue(maxsize=4)) as writer:
                writer.send_nowait(_heartbeat())
                raise ValueError("boom")
        assert len(call.written) == 1
        assert call.done_writing_called
        assert not _other_tasks()

    asyncio.run(scenario())


def test_writer_context_manager_keeps_the_original_error_when_close_also_fails() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall(error=_invalid_argument())
        with pytest.raises(ValueError, match="boom") as caught:
            async with MarketStreamWriter(call, asyncio.Queue(maxsize=4)) as writer:
                writer.send_nowait(_heartbeat())
                raise ValueError("boom")
        assert any("INVALID_ARGUMENT" in note for note in caught.value.__notes__)
        assert not _other_tasks()

    asyncio.run(scenario())


def test_writer_context_manager_raises_the_close_error_on_normal_exit() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall(error=_invalid_argument())
        with pytest.raises(EngineError, match="INVALID_ARGUMENT"):
            async with MarketStreamWriter(call, asyncio.Queue(maxsize=4)) as writer:
                writer.send_nowait(_heartbeat())
        assert call.cancelled
        assert not _other_tasks()

    asyncio.run(scenario())


def test_cancelling_a_blocked_close_cancels_the_pump_and_the_call() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall(block_writes=True)
        writer = MarketStreamWriter(call, asyncio.Queue(maxsize=4))

        async def use_writer() -> None:
            async with writer:
                writer.send_nowait(_heartbeat())

        task = asyncio.ensure_future(use_writer())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert call.cancelled
        assert not _other_tasks()

    asyncio.run(scenario())


def test_exiting_after_an_explicit_close_does_not_close_twice() -> None:
    async def scenario() -> None:
        call = _FakeStreamUnaryCall()
        async with MarketStreamWriter(call, asyncio.Queue(maxsize=4)) as writer:
            writer.send_nowait(_heartbeat())
            assert await writer.close() == 1
        assert not _other_tasks()

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


class _FakeSubscribeCall(_FakeUnaryStreamCall):
    def __init__(self, metadata: tuple[tuple[str, str], ...], code: grpc.StatusCode) -> None:
        super().__init__([])
        self._metadata = metadata
        self._code = code

    async def initial_metadata(self) -> tuple[tuple[str, str], ...]:  # type: ignore[override]
        return self._metadata

    async def code(self) -> grpc.StatusCode:
        return self._code

    async def details(self) -> str:
        return "detail"


def test_subscription_opens_only_with_the_subscribed_marker() -> None:
    async def scenario() -> None:
        call = _FakeSubscribeCall((("slipstream-subscribed", "1"),), grpc.StatusCode.OK)
        subscription = await Subscription.from_call(call)
        assert not call.cancelled
        await subscription.close()

    asyncio.run(scenario())


def test_subscription_without_the_marker_raises_even_if_the_call_ended_ok() -> None:
    async def scenario() -> None:
        call = _FakeSubscribeCall((), grpc.StatusCode.OK)
        with pytest.raises(EngineError, match="OK"):
            await Subscription.from_call(call)
        assert call.cancelled

    asyncio.run(scenario())


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


def test_replay_stream_book_trade_and_heartbeat_round_trip_visible_in_status(
    engine_address: str,
) -> None:
    async def scenario() -> None:
        channel = EngineChannel(engine_address)
        try:
            await channel.wait_ready(2.0)
            writer = await MarketStreamWriter.open(channel, None)
            book = BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 2.0),), "kraken")
            writer.send_nowait(book_event(book, _SEC))
            trades = TradeBatch("BTC/USD", False, ((100.0, 0.5),), "kraken")
            writer.send_nowait(trade_event(trades, 2 * _SEC))
            writer.send_nowait(heartbeat_event("kraken", 3 * _SEC))
            events = await writer.close()
            assert events == 3

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
                with pytest.raises(EngineError, match="FAILED_PRECONDITION"):
                    await Subscription.open(channel)
            finally:
                await first.close()
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_a_cancelled_subscribe_open_releases_the_call() -> None:
    # A timed-out open must not keep the engine's only subscriber slot on this channel.
    class _HangingCall:
        cancelled = False

        async def initial_metadata(self) -> Any:
            await asyncio.sleep(10)

        def cancel(self) -> None:
            self.cancelled = True

    call = _HangingCall()

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(Subscription.from_call(call), timeout=0.05)

    asyncio.run(run())
    assert call.cancelled
