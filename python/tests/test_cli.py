import json
from pathlib import Path

import pytest

from slipstream.cli import build_parser, format_comparison, format_summary, main
from slipstream.models import Fill
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


def test_algo_flags_parse() -> None:
    args = build_parser().parse_args(
        [
            *BASE,
            "--qty",
            "1",
            "--algo",
            "almgren_chriss",
            "--urgency",
            "high",
            "--risk-aversion",
            "0.5",
        ]
    )
    assert (args.algo, args.urgency, args.risk_aversion) == ("almgren_chriss", "high", 0.5)
    assert build_parser().parse_args([*BASE, "--qty", "1"]).algo == "twap"


@pytest.mark.parametrize("value", ["0", "0.51", "-0.1", "nan", "x"])
def test_participation_is_bounded(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", "1", "--participation", value])


def test_compare_parses_algo_list() -> None:
    args = build_parser().parse_args(
        [
            "compare",
            "--side",
            "buy",
            "--qty",
            "1",
            "--duration",
            "6",
            "--slices",
            "3",
            "--algos",
            "twap,pov,twap",
        ]
    )
    assert args.algos == ["twap", "pov"]
    assert args.file is None


@pytest.mark.parametrize("value", ["", "twap,bogus", ","])
def test_compare_rejects_bad_algo_lists(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "compare",
                "--side",
                "buy",
                "--qty",
                "1",
                "--duration",
                "6",
                "--slices",
                "3",
                "--algos",
                value,
            ]
        )


def test_format_comparison_table() -> None:
    statuses = [
        pb.OrderStatus(
            order_id="a",
            algo="twap",
            state=pb.ORDER_STATE_COMPLETED,
            filled_qty=1.0,
            avg_fill_price=101.0,
            slippage_bps=100.0,
            immediate_cost_bps=150.0,
        ),
        pb.OrderStatus(
            order_id="b",
            algo="pov",
            state=pb.ORDER_STATE_HALTED,
            filled_qty=0.5,
            avg_fill_price=101.0,
            slippage_bps=100.0,
            immediate_cost_bps=150.0,
        ),
    ]
    fills = [Fill("a", 1, 0.5, 101.0), Fill("a", 2, 0.5, 101.0), Fill("b", 1, 0.5, 101.0)]
    lines = format_comparison(statuses, fills).splitlines()
    assert lines[0].split() == [
        "algo",
        "state",
        "filled",
        "avg",
        "px",
        "slip",
        "bps",
        "1-shot",
        "bps",
        "saved",
        "bps",
        "fills",
    ]
    assert lines[1].split() == [
        "twap",
        "COMPLETED",
        "1",
        "101.00",
        "100.00",
        "150.00",
        "50.00",
        "2",
    ]
    assert lines[2].split() == ["pov", "HALTED", "0.5", "101.00", "100.00", "150.00", "50.00", "1"]


def test_summary_shows_algo() -> None:
    assert "algo         vwap" in format_summary(pb.OrderStatus(order_id="o", algo="vwap"))


def test_replay_without_ohlc_fails_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLIPSTREAM_PAPER_MODE", raising=False)
    file = tmp_path / "no-ohlc.jsonl"
    file.write_text(
        json.dumps({"recv_ns": 1, "msg": {"channel": "heartbeat"}}) + "\n", encoding="utf-8"
    )
    code = main(
        [
            "replay",
            "--file",
            str(file),
            "--side",
            "buy",
            "--qty",
            "1",
            "--duration",
            "6",
            "--slices",
            "3",
            "--algo",
            "vwap",
            "--engine",
            "127.0.0.1:1",
        ]
    )
    assert code == 1
    assert "OHLC" in capsys.readouterr().err
