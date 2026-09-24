import json
import logging
import math
from pathlib import Path

import pytest

from slipstream.calibration import CalibrationData, estimate_eta, estimate_sigma
from slipstream.engine_client import EngineClient
from slipstream.models import OrderSpec
from slipstream.replay import read_calibration, read_replay, run_replay
from slipstream.runner import ExecutionRunner
from slipstream.v1 import execution_pb2 as pb

DAY0 = 1_700_006_400
T0_S = DAY0 + 898
NS = 1_000_000_000
ASKS = [(100010.0, 0.01), (100020.0, 0.01), (100030.0, 0.02), (100040.0, 1.0)]
BIDS = [(99990.0, 1.0), (99980.0, 1.0)]


def ohlc_line(interval: int, rows: list[list[object]]) -> str:
    data = {"error": [], "result": {"XXBTZUSD": rows, "last": 0}}
    return json.dumps({"kind": "ohlc", "interval": interval, "data": data})


def row(time_s: int, close: float, volume: float) -> list[object]:
    c = f"{close:.1f}"
    return [time_s, c, c, c, c, c, f"{volume:.8f}", 1]


def msg_line(offset_s: float, msg: dict[str, object]) -> str:
    return json.dumps({"recv_ns": int((T0_S + offset_s) * NS), "msg": msg})


def book_snapshot() -> dict[str, object]:
    return {
        "channel": "book",
        "type": "snapshot",
        "data": [
            {
                "symbol": "BTC/USD",
                "bids": [{"price": p, "qty": q} for p, q in BIDS],
                "asks": [{"price": p, "qty": q} for p, q in ASKS],
            }
        ],
    }


def trade(qty: float) -> dict[str, object]:
    return {
        "channel": "trade",
        "type": "update",
        "data": [{"symbol": "BTC/USD", "price": 100000.0, "qty": qty}],
    }


def build_session(path: Path) -> None:
    fifteen = [row(DAY0 - 86_400, 100000.0, 30.0), row(DAY0 - 86_400 + 900, 100000.0, 10.0)]
    one = [row(DAY0 + 60 * i, 100000.0 + (i % 2), 1.0) for i in range(61)]
    heartbeat = {"channel": "heartbeat"}
    lines = [
        ohlc_line(15, fifteen),
        ohlc_line(1, one),
        msg_line(0.0, {"method": "subscribe", "success": True, "result": {}}),
        msg_line(0.0, book_snapshot()),
        msg_line(0.5, trade(0.02)),
        msg_line(1.0, heartbeat),
        msg_line(1.5, trade(0.06)),
        msg_line(2.0, heartbeat),
        msg_line(2.5, trade(0.08)),
        msg_line(3.0, heartbeat),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fills_for(runner: ExecutionRunner, order_id: str) -> list[float]:
    return [fill.qty for fill in runner.fills if fill.order_id == order_id]


def test_four_schedules_end_to_end(engine_address: str, tmp_path: Path) -> None:
    session = tmp_path / "session.jsonl"
    build_session(session)
    bars = read_calibration(session)
    calibration = CalibrationData(bars[15], bars[1])

    sigma = estimate_sigma(calibration.bars_1m)
    eta = estimate_eta(ASKS, 1.0)
    risk_aversion = (math.cosh(0.5) - 1.0) * 2.0 * eta / sigma**2

    specs = [
        OrderSpec("twap", "buy", 0.04, 4, 4, algo="twap"),
        OrderSpec("vwap", "buy", 0.04, 4, 4, algo="vwap"),
        OrderSpec("pov", "buy", 0.04, 4, 4, algo="pov", participation=0.25),
        OrderSpec("ac", "buy", 0.04, 4, 4, algo="almgren_chriss", risk_aversion=risk_aversion),
    ]
    client = EngineClient(engine_address)
    try:
        client.wait_ready()
        runner = ExecutionRunner(client, specs, "BTC/USD", logging.getLogger("test"), calibration)
        run_replay(runner, read_replay(session))
        statuses = {status.order_id: status for status in runner.order_statuses()}
    finally:
        client.close()

    assert {oid: pb.OrderState.Name(s.state) for oid, s in statuses.items()} == {
        oid: "ORDER_STATE_COMPLETED" for oid in ("twap", "vwap", "pov", "ac")
    }
    assert {oid: s.algo for oid, s in statuses.items()} == {
        "twap": "twap",
        "vwap": "vwap",
        "pov": "pov",
        "ac": "almgren_chriss",
    }
    assert fills_for(runner, "twap") == pytest.approx([0.01] * 4)
    assert fills_for(runner, "vwap") == pytest.approx([0.015, 0.015, 0.005, 0.005])
    assert fills_for(runner, "pov") == pytest.approx([0.005, 0.015, 0.02])
    cumulative = [1 - math.sinh(0.5 * (4 - k)) / math.sinh(2.0) for k in (1, 2, 3)] + [1.0]
    expected_ac = [0.04 * (b - a) for a, b in zip([0.0, *cumulative[:-1]], cumulative, strict=True)]
    assert fills_for(runner, "ac") == pytest.approx(expected_ac, abs=1e-9)
    assert fills_for(runner, "ac")[0] > 0.01
