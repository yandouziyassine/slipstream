from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from slipstream.engine_client import EngineError
from slipstream.engine_stream import EngineChannel, Subscription
from slipstream.models import MarketDataError, OrderSpec, Venue
from slipstream.replay import ReplayError, read_replay
from slipstream.runner import OrderRejectedError
from slipstream.session import (
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
    from slipstream.session import ReplaySession

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
