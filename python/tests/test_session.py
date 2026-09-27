from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from slipstream.book import LocalBook
from slipstream.calibration import CalibrationData, CalibrationError
from slipstream.engine_stream import EngineChannel, EngineError, Subscription
from slipstream.kraken_rest import Bar
from slipstream.models import (
    BookUpdate,
    MarketDataError,
    OrderSpec,
    PovParams,
    ScheduleParams,
    TwapParams,
    Venue,
)
from slipstream.replay import ReplayError, read_replay
from slipstream.session import (
    OrderRejectedError,
    ReplaySession,
    SessionResult,
    check_clock_mode,
    clock_mode_mismatch,
    run_replay_session,
)
from slipstream.v1 import execution_pb2 as pb

_SEC = 1_000_000_000
_T0 = 1_700_000_000 * _SEC
_DAY_S = 86_400


def _snapshot(symbol: str = "BTC/USD") -> dict[str, object]:
    return {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": symbol,
                "bids": [{"price": 99990.0, "qty": 1.0}],
                "asks": [{"price": 100010.0, "qty": 1.0}],
            }
        ],
    }


def _write(path: Path, records: Sequence[tuple[int, dict[str, object], str]]) -> Path:
    lines = [json.dumps({"recv_ns": ns, "venue": venue, "msg": msg}) for ns, msg, venue in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run(
    address: str,
    specs: Sequence[OrderSpec],
    records: Iterable[tuple[int, str, Venue]],
    venues: Sequence[Venue] = ("kraken",),
) -> SessionResult:
    async def scenario() -> SessionResult:
        channel = EngineChannel(address)
        try:
            await channel.wait_ready(5.0)
            return await run_replay_session(
                channel,
                specs,
                "BTC/USD",
                logging.getLogger("test"),
                records=records,
                venues=venues,
                fee_bps=await channel.venue_fees(),
            )
        finally:
            await channel.close()

    return asyncio.run(scenario())


def test_clock_mode_mismatch_names_both_flags() -> None:
    assert clock_mode_mismatch(pb.CLOCK_MODE_REPLAY, pb.CLOCK_MODE_REPLAY) is None
    assert clock_mode_mismatch(pb.CLOCK_MODE_LIVE, pb.CLOCK_MODE_REPLAY) == (
        "engine runs with --clock live but replay needs --clock replay"
    )
    assert clock_mode_mismatch(pb.CLOCK_MODE_REPLAY, pb.CLOCK_MODE_LIVE) == (
        "engine runs with --clock replay but live needs --clock live"
    )
    assert clock_mode_mismatch(pb.CLOCK_MODE_UNSPECIFIED, pb.CLOCK_MODE_LIVE) == (
        "engine reports no clock mode but live needs --clock live"
    )


def _check(address: str, expected: pb.ClockMode.ValueType) -> None:
    async def scenario() -> None:
        channel = EngineChannel(address)
        try:
            await channel.wait_ready(5.0)
            await check_clock_mode(channel, expected)
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_check_clock_mode_accepts_the_matching_engine(engine_address: str) -> None:
    _check(engine_address, pb.CLOCK_MODE_REPLAY)


def test_check_clock_mode_rejects_a_live_engine_for_replay(live_engine_address: str) -> None:
    with pytest.raises(EngineError, match="--clock live but replay needs --clock replay"):
        _check(live_engine_address, pb.CLOCK_MODE_REPLAY)


def test_check_clock_mode_rejects_a_replay_engine_for_live(engine_address: str) -> None:
    with pytest.raises(EngineError, match="--clock replay but live needs --clock live"):
        _check(engine_address, pb.CLOCK_MODE_LIVE)


def test_session_submits_at_the_snapshot_and_collects_fills_in_engine_order(
    engine_address: str, tmp_path: Path
) -> None:
    file = _write(
        tmp_path / "s.jsonl",
        [
            (_T0, _snapshot(), "kraken"),
            (_T0 + 1 * _SEC, {"channel": "heartbeat"}, "kraken"),
            (_T0 + 2 * _SEC, {"channel": "heartbeat"}, "kraken"),
            (_T0 + 4 * _SEC, {"channel": "heartbeat"}, "kraken"),
        ],
    )
    result = _run(engine_address, [OrderSpec("s-1", "buy", 0.03, 6, 3)], read_replay(file))

    assert [(f.order_id, f.ts_ns, f.venue) for f in result.fills] == [
        ("s-1", _T0, "kraken"),
        ("s-1", _T0 + 2 * _SEC, "kraken"),
        ("s-1", _T0 + 4 * _SEC, "kraken"),
    ]
    assert [f.qty for f in result.fills] == pytest.approx([0.01, 0.01, 0.01])
    assert [s.order_id for s in result.statuses] == ["s-1"]
    assert result.statuses[0].state == pb.ORDER_STATE_COMPLETED
    assert result.stats.events >= 4


def test_orders_still_working_at_end_of_file_resolve_at_their_deadline(
    engine_address: str, tmp_path: Path
) -> None:
    file = _write(tmp_path / "s.jsonl", [(_T0, _snapshot(), "kraken")])
    result = _run(engine_address, [OrderSpec("late-1", "buy", 0.03, 6, 3)], read_replay(file))

    # One closing tick at the deadline: the schedule's target is then the whole order.
    assert [f.ts_ns for f in result.fills] == [_T0, _T0 + 6 * _SEC]
    assert [f.qty for f in result.fills] == pytest.approx([0.01, 0.02])
    assert result.statuses[0].state == pb.ORDER_STATE_COMPLETED


def test_closing_ticks_never_jump_more_than_a_day(engine_address: str, tmp_path: Path) -> None:
    file = _write(tmp_path / "s.jsonl", [(_T0, _snapshot(), "kraken")])
    duration_s = 2 * _DAY_S + 5
    result = _run(
        engine_address, [OrderSpec("long-1", "buy", 0.03, duration_s, 3)], read_replay(file)
    )

    day = _DAY_S * _SEC
    assert [f.ts_ns for f in result.fills] == [_T0, _T0 + day, _T0 + 2 * day]
    assert [f.qty for f in result.fills] == pytest.approx([0.01, 0.01, 0.01])
    assert result.statuses[0].state == pb.ORDER_STATE_COMPLETED


def test_no_snapshot_means_nothing_is_submitted(engine_address: str, tmp_path: Path) -> None:
    file = _write(tmp_path / "s.jsonl", [(_T0, {"channel": "heartbeat"}, "kraken")])
    result = _run(engine_address, [OrderSpec("none-1", "buy", 0.03, 6, 3)], read_replay(file))
    assert result.statuses == []
    assert result.fills == []


def test_skipped_venue_records_still_count_for_timestamp_order(
    engine_address: str, tmp_path: Path
) -> None:
    file = _write(
        tmp_path / "s.jsonl",
        [
            (_T0 + _SEC, {"channel": "heartbeat"}, "coinbase"),
            (_T0, _snapshot(), "kraken"),
        ],
    )
    with pytest.raises(ReplayError, match="non-decreasing"):
        _run(engine_address, [OrderSpec("o-1", "buy", 0.03, 6, 3)], read_replay(file))


def test_unexpected_symbol_is_market_data_error(engine_address: str, tmp_path: Path) -> None:
    file = _write(tmp_path / "s.jsonl", [(_T0, _snapshot("ETH/USD"), "kraken")])
    with pytest.raises(MarketDataError, match="unexpected symbol"):
        _run(engine_address, [OrderSpec("o-1", "buy", 0.03, 6, 3)], read_replay(file))


def test_rejected_order_raises_with_the_engine_reason(engine_address: str, tmp_path: Path) -> None:
    file = _write(tmp_path / "s.jsonl", [(_T0, _snapshot(), "kraken")])
    # The fixture engine allows at most 10,000 notional per order; 0.5 BTC is about 50,000.
    with pytest.raises(OrderRejectedError, match="order big-1 rejected: "):
        _run(engine_address, [OrderSpec("big-1", "buy", 0.5, 6, 3)], read_replay(file))


@pytest.mark.parametrize("with_snapshot", [True, False])
def test_a_rejected_subscription_is_fatal(
    engine_address: str, tmp_path: Path, with_snapshot: bool
) -> None:
    heartbeat: dict[str, object] = {"channel": "heartbeat"}
    file = _write(
        tmp_path / "s.jsonl", [(_T0, _snapshot() if with_snapshot else heartbeat, "kraken")]
    )

    async def scenario() -> None:
        channel = EngineChannel(engine_address)
        try:
            await channel.wait_ready(5.0)
            other = await Subscription.open(channel)
            try:
                await run_replay_session(
                    channel,
                    [OrderSpec("o-1", "buy", 0.03, 6, 3)],
                    "BTC/USD",
                    logging.getLogger("test"),
                    records=read_replay(file),
                )
            finally:
                await other.close()
        finally:
            await channel.close()

    with pytest.raises(EngineError, match="FAILED_PRECONDITION"):
        asyncio.run(scenario())


def test_session_rejects_bad_venue_lists() -> None:
    async def scenario() -> None:
        channel = EngineChannel("127.0.0.1:1")
        try:
            for venues in ((), ("kraken", "kraken")):
                with pytest.raises(ValueError, match="venue"):
                    await run_replay_session(
                        channel,
                        [OrderSpec("o-1", "buy", 0.03, 6, 3)],
                        "BTC/USD",
                        logging.getLogger("test"),
                        records=[],
                        venues=venues,
                    )
        finally:
            await channel.close()

    asyncio.run(scenario())


class _FailingCloseWriter:
    async def close(self) -> int:
        raise EngineError("market stream failed: UNAVAILABLE: engine gone")


def _bare_replay_session() -> Any:
    session = ReplaySession.__new__(ReplaySession)
    session._log = logging.getLogger("test")
    return session


def test_a_data_error_stays_the_root_cause_when_closing_also_fails() -> None:
    cause = MarketDataError("unexpected symbol 'ETH/USD'")
    asyncio.run(_bare_replay_session()._abandon(_FailingCloseWriter(), cause))
    assert any("market stream" in note for note in cause.__notes__)


def test_an_engine_side_failure_is_replaced_by_the_stream_rejection() -> None:
    cause = EngineError("timed out waiting for the engine to catch up")
    with pytest.raises(EngineError, match="market stream failed"):
        asyncio.run(_bare_replay_session()._abandon(_FailingCloseWriter(), cause))


# Unit tests of the replay mapping: which engine events each record becomes, and when and how
# the orders are submitted. They need no engine: the channel only records submits.


class _RecordingChannel:
    def __init__(self, reject_ids: frozenset[str] = frozenset()) -> None:
        self.submits: list[tuple[OrderSpec, int, ScheduleParams | None]] = []
        self._reject_ids = reject_ids

    async def submit(
        self, spec: OrderSpec, start_ns: int, params: ScheduleParams | None = None
    ) -> tuple[bool, str]:
        self.submits.append((spec, start_ns, params))
        if spec.order_id in self._reject_ids:
            return False, "position limit exceeded"
        return True, ""


_SPEC = OrderSpec("o-1", "buy", 1.0, 4, 4)
_HEARTBEAT = json.dumps({"channel": "heartbeat"})


def _kraken_snapshot(symbol: str = "BTC/USD") -> str:
    return json.dumps(
        {
            "channel": "book",
            "type": "snapshot",
            "data": [
                {
                    "symbol": symbol,
                    "bids": [{"price": 99.0, "qty": 1.0}],
                    "asks": [{"price": 101.0, "qty": 1.0}],
                }
            ],
        }
    )


def _kraken_delta() -> str:
    return json.dumps(
        {
            "channel": "book",
            "type": "update",
            "data": [{"symbol": "BTC/USD", "bids": [], "asks": [{"price": 100.5, "qty": 2.0}]}],
        }
    )


def _kraken_trade(msg_type: str = "update", symbol: str = "BTC/USD") -> str:
    return json.dumps(
        {
            "channel": "trade",
            "type": msg_type,
            "data": [{"symbol": symbol, "price": 100.0, "qty": 1.0}],
        }
    )


def _coinbase_snapshot() -> str:
    return json.dumps(
        {
            "channel": "l2_data",
            "sequence_num": 0,
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "BTC-USD",
                    "updates": [
                        {"side": "bid", "price_level": "99", "new_quantity": "1"},
                        {"side": "offer", "price_level": "101", "new_quantity": "1"},
                    ],
                }
            ],
        }
    )


def _session(
    channel: _RecordingChannel,
    specs: Sequence[OrderSpec] = (_SPEC,),
    venues: Sequence[Venue] = ("kraken",),
    calibration: CalibrationData | None = None,
    fee_bps: dict[Venue, float] | None = None,
) -> ReplaySession:
    return ReplaySession(
        channel,  # type: ignore[arg-type]
        specs,
        "BTC/USD",
        logging.getLogger("test"),
        calibration,
        venues,
        fee_bps=fee_bps,
    )


def _kinds(events: Sequence[pb.MarketEvent]) -> list[str | None]:
    return [event.WhichOneof("event") for event in events]


def _submitted(session: ReplaySession, now_ns: int = 100) -> ReplaySession:
    _, due = session._events(_kraken_snapshot(), now_ns, "kraken")
    assert due
    asyncio.run(session._submit_all(now_ns))
    return session


def test_a_book_record_becomes_a_book_event_at_its_receive_time() -> None:
    events, due = _session(_RecordingChannel())._events(_kraken_snapshot(), 1234, "kraken")
    assert _kinds(events) == ["book"]
    assert events[0].book.recv_ns == 1234
    assert events[0].book.venue == "kraken"
    assert due


def test_orders_wait_for_a_snapshot_from_every_venue_while_trades_flow() -> None:
    session = _session(_RecordingChannel(), venues=("kraken", "coinbase"))
    _, due = session._events(_kraken_snapshot(), 100, "kraken")
    assert not due
    events, due = session._events(_kraken_trade(), 150, "kraken")
    assert _kinds(events) == ["trades"]
    assert (events[0].trades.venue, events[0].trades.recv_ns) == ("kraken", 150)
    assert not due
    events, due = session._events(_coinbase_snapshot(), 300, "coinbase")
    assert [event.book.venue for event in events] == ["coinbase"]
    assert due


def test_trade_snapshots_are_history_and_never_forwarded() -> None:
    session = _session(_RecordingChannel())
    events, _ = session._events(_kraken_trade("snapshot"), 1, "kraken")
    assert events == []


def test_another_symbol_is_market_data_error() -> None:
    session = _session(_RecordingChannel())
    with pytest.raises(MarketDataError, match="symbol"):
        session._events(_kraken_trade(symbol="ETH/USD"), 1, "kraken")
    with pytest.raises(MarketDataError, match="symbol"):
        session._events(_kraken_snapshot("ETH/USD"), 1, "kraken")


def test_submits_every_spec_with_calibrated_params_at_the_snapshot_time() -> None:
    channel = _RecordingChannel()
    specs = [
        OrderSpec("a", "buy", 1.0, 4, 4),
        OrderSpec("b", "buy", 1.0, 4, 4, algo="pov", participation=0.3),
    ]
    _submitted(_session(channel, specs))
    assert [(s.order_id, start, p) for s, start, p in channel.submits] == [
        ("a", 100, TwapParams()),
        ("b", 100, PovParams(0.3)),
    ]


def test_calibrated_algo_without_data_fails_before_submitting() -> None:
    channel = _RecordingChannel()
    session = _session(channel, [OrderSpec("v", "buy", 1.0, 4, 4, algo="vwap")])
    with pytest.raises(CalibrationError):
        _submitted(session)
    assert channel.submits == []


def _bars_1m(count: int) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            time_s=i * 60,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0 + (0.05 if i % 2 == 0 else -0.05) + 0.01 * i,
            vwap=100.0,
            volume=1.0,
            count=1,
        )
        for i in range(count)
    )


def _bars_15m(count: int) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            time_s=i * 900,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            vwap=100.0,
            volume=10.0,
            count=5,
        )
        for i in range(count)
    )


def test_calibrates_every_spec_before_submitting_any() -> None:
    channel = _RecordingChannel()
    specs = [
        OrderSpec("t", "buy", 1.0, 4, 4, algo="twap"),
        OrderSpec("ac", "buy", 1.0, 4, 4, algo="almgren_chriss"),
    ]
    session = _session(channel, specs, calibration=CalibrationData(_bars_15m(4), _bars_1m(61)))
    # The snapshot has only one ask level, too thin to calibrate Almgren-Chriss impact.
    with pytest.raises(CalibrationError):
        _submitted(session)
    assert channel.submits == []


def test_a_rejection_leaves_earlier_orders_submitted() -> None:
    # Documented limitation: "a" was accepted before "b" was rejected, and stays working.
    channel = _RecordingChannel(reject_ids=frozenset({"b"}))
    specs = [OrderSpec("a", "buy", 1.0, 4, 4), OrderSpec("b", "buy", 1.0, 4, 4)]
    with pytest.raises(OrderRejectedError, match="order b rejected: position limit"):
        _submitted(_session(channel, specs))
    assert [s.order_id for s, _, _ in channel.submits] == ["a", "b"]


def _spy_schedule_params(monkeypatch: pytest.MonkeyPatch) -> list[BookUpdate]:
    seen: list[BookUpdate] = []

    def spy(
        spec: OrderSpec, book: BookUpdate, start_ns: int, data: CalibrationData | None
    ) -> ScheduleParams:
        seen.append(book)
        return TwapParams()

    monkeypatch.setattr("slipstream.session.schedule_params", spy)
    return seen


def test_calibration_uses_the_consolidated_fee_adjusted_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_schedule_params(monkeypatch)
    session = _session(
        _RecordingChannel(),
        venues=("kraken", "coinbase"),
        fee_bps={"kraken": 0.0, "coinbase": 100.0},
    )
    session._events(_kraken_snapshot(), 100, "kraken")
    _, due = session._events(_coinbase_snapshot(), 200, "coinbase")
    assert due
    asyncio.run(session._submit_all(200))
    (book,) = seen
    # pytest.approx() does not support nesting past one level, so the per-price tolerance is
    # applied by hand instead of wrapping the whole tuple of tuples.
    assert book.asks == ((pytest.approx(101.0), 1.0), (pytest.approx(102.01), 1.0))
    assert book.bids == ((pytest.approx(99.0), 1.0), (pytest.approx(98.01), 1.0))


def test_calibration_book_includes_deltas_since_the_first_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_schedule_params(monkeypatch)
    session = _session(_RecordingChannel(), venues=("kraken", "coinbase"))
    session._events(_kraken_snapshot(), 100, "kraken")
    session._events(_kraken_delta(), 150, "kraken")
    session._events(_coinbase_snapshot(), 200, "coinbase")
    asyncio.run(session._submit_all(200))
    (book,) = seen
    assert book.asks == ((100.5, 2.0), (101.0, 1.0), (101.0, 1.0))


def test_local_books_stop_updating_after_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = LocalBook.apply

    def spy_apply(self: LocalBook, update: BookUpdate) -> None:
        nonlocal calls
        calls += 1
        original(self, update)

    monkeypatch.setattr(LocalBook, "apply", spy_apply)
    session = _submitted(_session(_RecordingChannel()))
    assert calls == 1
    events, due = session._events(_kraken_delta(), 200, "kraken")
    assert calls == 1  # the engine still receives the delta
    assert _kinds(events) == ["book"]
    assert not due


def test_heartbeats_go_to_the_engine_with_their_venue_and_time() -> None:
    # The engine itself bounds how long heartbeats keep a quiet venue fresh.
    session = _session(_RecordingChannel(), venues=("kraken", "coinbase"))
    for now_ns in (50, 100 + 31 * _SEC):
        events, due = session._events(_HEARTBEAT, now_ns, "kraken")
        assert _kinds(events) == ["heartbeat"]
        assert (events[0].heartbeat.venue, events[0].heartbeat.recv_ns) == ("kraken", now_ns)
        assert not due


def test_a_trade_after_submit_is_one_event_at_its_own_time() -> None:
    session = _submitted(_session(_RecordingChannel()))
    events, _ = session._events(_kraken_trade(), 200, "kraken")
    assert _kinds(events) == ["trades"]
    assert events[0].trades.recv_ns == 200
    # Historical prints only move time forward once orders are working.
    events, _ = session._events(_kraken_trade("snapshot"), 300, "kraken")
    assert _kinds(events) == ["tick"]
    assert events[0].tick.now_ns == 300


def test_fill_log_carries_venue_and_fee(caplog: pytest.LogCaptureFixture) -> None:
    session = _session(_RecordingChannel())
    with caplog.at_level(logging.INFO):
        session._record_fill(
            pb.Fill(order_id="o-1", ts_ns=5, qty=0.25, price=101.0, venue="coinbase", fee=0.02)
        )
    fields = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "fill"]
    assert fields[0]["venue"] == "coinbase"
    assert fields[0]["fee"] == 0.02
    assert [(f.venue, f.fee) for f in session.fills] == [("coinbase", 0.02)]
