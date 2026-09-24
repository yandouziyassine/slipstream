from pathlib import Path

import pytest

from slipstream.cli import build_parser, format_summary, main
from slipstream.v1 import execution_pb2 as pb

BASE = ["replay", "--file", "x.jsonl", "--side", "buy", "--duration", "6", "--slices", "3"]


@pytest.mark.parametrize("qty", ["0", "-1", "nan", "inf", "abc"])
def test_rejects_bad_quantity(qty: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", qty])


@pytest.mark.parametrize(
    "extra",
    [["--symbol", "btc/usd"], ["--order-id", "bad id"], ["--slices", "0"]],
)
def test_rejects_bad_arguments(extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", "1", *extra])


def test_parses_valid_arguments() -> None:
    args = build_parser().parse_args([*BASE, "--qty", "0.06"])
    assert args.qty == 0.06
    assert args.symbol == "BTC/USD"


def test_format_summary() -> None:
    status = pb.OrderStatus(
        order_id="o-1",
        state=pb.ORDER_STATE_COMPLETED,
        total_qty=0.06,
        filled_qty=0.06,
        avg_fill_price=100008.333,
        arrival_mid=100000.0,
        slippage_bps=0.8333,
        immediate_cost_bps=1.6667,
    )
    text = format_summary(status)
    assert "COMPLETED" in text
    assert "0.83 bps" in text
    assert "1.67 bps" in text
    assert "saved        0.83 bps" in text


def test_live_mode_env_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLIPSTREAM_PAPER_MODE", "false")
    assert main([*BASE, "--qty", "1"]) == 1


def test_non_loopback_engine_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLIPSTREAM_PAPER_MODE", raising=False)
    assert main([*BASE, "--qty", "1", "--engine", "10.0.0.1:50051"]) == 1


def test_record_refuses_existing_file_before_any_network(tmp_path: Path) -> None:
    existing = tmp_path / "s.jsonl"
    existing.write_text("keep me", encoding="utf-8")
    assert main(["record", "--duration", "5", "--out", str(existing)]) == 1
    assert existing.read_text(encoding="utf-8") == "keep me"
