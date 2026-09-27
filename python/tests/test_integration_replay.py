import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from slipstream.engine_client import EngineClient
from slipstream.models import OrderSpec
from slipstream.replay import read_replay, run_replay
from slipstream.runner import ExecutionRunner
from slipstream.session import SessionResult
from slipstream.v1 import execution_pb2 as pb

FIXTURE = Path(__file__).parent / "fixtures" / "kraken_btcusd_replay.jsonl"


def test_replay_twap_end_to_end_against_real_engine(engine_address: str) -> None:
    client = EngineClient(engine_address)
    try:
        client.wait_ready()
        spec = OrderSpec("replay-1", "buy", 0.06, 6, 3)
        runner = ExecutionRunner(client, spec, "BTC/USD", logging.getLogger("test"))
        run_replay(runner, read_replay(FIXTURE))

        status = runner.order_status()
        assert status is not None
        assert status.state == pb.ORDER_STATE_COMPLETED
        assert [round(f.price, 2) for f in runner.fills] == [100010.0, 100010.0, 100005.0]
        assert status.filled_qty == pytest.approx(0.06)
        assert status.arrival_mid == pytest.approx(100000.0)
        assert status.slippage_bps == pytest.approx(0.8333, abs=1e-3)
        assert status.immediate_cost_bps == pytest.approx(1.6667, abs=1e-3)
        assert status.slippage_bps < status.immediate_cost_bps
    finally:
        client.close()


def test_replay_twap_end_to_end_through_the_session(
    engine_address: str, replay_session: Callable[..., SessionResult]
) -> None:
    spec = OrderSpec("replay-1", "buy", 0.06, 6, 3)
    result = replay_session(engine_address, [spec], FIXTURE)

    [status] = result.statuses
    assert status.state == pb.ORDER_STATE_COMPLETED
    assert [round(f.price, 2) for f in result.fills] == [100010.0, 100010.0, 100005.0]
    assert status.filled_qty == pytest.approx(0.06)
    assert status.arrival_mid == pytest.approx(100000.0)
    assert status.slippage_bps == pytest.approx(0.8333, abs=1e-3)
    assert status.immediate_cost_bps == pytest.approx(1.6667, abs=1e-3)
    assert status.slippage_bps < status.immediate_cost_bps
