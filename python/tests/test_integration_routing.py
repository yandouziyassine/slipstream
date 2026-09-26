import logging
from pathlib import Path

import pytest

from slipstream.cli import routing_gain_bps
from slipstream.engine_client import EngineClient
from slipstream.models import OrderSpec
from slipstream.replay import read_replay, run_replay
from slipstream.runner import ExecutionRunner
from slipstream.v1 import execution_pb2 as pb

FIXTURE = Path(__file__).parent / "fixtures" / "two_venue_btcusd_replay.jsonl"


def test_routes_across_two_venues_by_all_in_price(two_venue_engine_address: str) -> None:
    client = EngineClient(two_venue_engine_address)
    try:
        client.wait_ready()
        fees = client.venue_fees()
        assert fees == {"kraken": 0.0, "coinbase": 1.0}
        runner = ExecutionRunner(
            client,
            OrderSpec("route-1", "buy", 0.05, 6, 1),
            "BTC/USD",
            logging.getLogger("test"),
            venues=("kraken", "coinbase"),
            fee_bps=fees,
        )
        run_replay(runner, read_replay(FIXTURE))

        legs = [(f.venue, f.qty, f.price, f.fee) for f in runner.fills]
        assert legs == [
            ("kraken", pytest.approx(0.04), pytest.approx(100020.0), pytest.approx(0.0)),
            ("coinbase", pytest.approx(0.01), pytest.approx(100015.0), pytest.approx(0.100015)),
        ]
        status = runner.order_status()
        assert status is not None
        assert status.state == pb.ORDER_STATE_COMPLETED
        assert status.arrival_mid == pytest.approx(100002.5)
        assert status.avg_fill_price == pytest.approx(100019.0)
        assert status.slippage_bps == pytest.approx(1.64996, abs=1e-4)
        assert status.fees_paid == pytest.approx(0.100015)
        assert status.fees_bps == pytest.approx(0.19999, abs=1e-4)
        assert status.routed_all_in_bps == pytest.approx(1.84998, abs=1e-4)
        costs = {c.venue: (c.available, c.all_in_bps) for c in status.venue_costs}
        assert costs["kraken"] == (True, pytest.approx(1.94995, abs=1e-4))
        assert costs["coinbase"] == (True, pytest.approx(3.05015, abs=1e-4))
        assert routing_gain_bps(status) == pytest.approx(0.09997, abs=1e-4)
    finally:
        client.close()


def test_kraken_only_replay_of_two_venue_file_uses_kraken_alone(engine_address: str) -> None:
    client = EngineClient(engine_address)
    try:
        client.wait_ready()
        runner = ExecutionRunner(
            client, OrderSpec("solo-1", "buy", 0.05, 6, 1), "BTC/USD", logging.getLogger("test")
        )
        run_replay(runner, read_replay(FIXTURE))
        assert {f.venue for f in runner.fills} == {"kraken"}
        status = runner.order_status()
        assert status is not None
        assert status.avg_fill_price == pytest.approx(100022.0)
    finally:
        client.close()
