import json
from pathlib import Path

import pytest

from slipstream.cli import build_parser, format_comparison, format_summary, main, routing_gain_bps
from slipstream.models import Fill
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules, VenueRulesError

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
        "fee",
        "bps",
        "all-in",
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
        "0.00",
        "0.00",
        "2",
    ]
    assert lines[2].split() == [
        "pov",
        "HALTED",
        "0.5",
        "101.00",
        "100.00",
        "150.00",
        "50.00",
        "0.00",
        "0.00",
        "1",
    ]


def test_summary_shows_algo() -> None:
    assert "algo         vwap" in format_summary(pb.OrderStatus(order_id="o", algo="vwap"))


def test_order_id_not_allowed_on_compare() -> None:
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
                "--order-id",
                "x",
            ]
        )


def test_order_id_still_allowed_on_replay() -> None:
    args = build_parser().parse_args([*BASE, "--qty", "1", "--order-id", "myid"])
    assert args.order_id == "myid"


def test_replay_twap_without_ohlc_fails_only_because_engine_unreachable(
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
            "twap",
            "--engine",
            "127.0.0.1:1",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "not reachable" in err
    assert "OHLC" not in err


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


@pytest.mark.parametrize("value", ["", "binance", "kraken,binance", ","])
def test_rejects_bad_venues(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([*BASE, "--qty", "1", "--venues", value])


def test_parses_and_dedupes_venues() -> None:
    args = build_parser().parse_args([*BASE, "--qty", "1", "--venues", "coinbase, kraken,coinbase"])
    assert args.venues == ("coinbase", "kraken")


def test_venues_default_to_kraken() -> None:
    assert build_parser().parse_args([*BASE, "--qty", "1"]).venues == ("kraken",)


@pytest.mark.parametrize("command", ["live", "compare", "record"])
def test_every_feed_command_accepts_venues(command: str) -> None:
    base = {
        "live": ["live", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3"],
        "compare": ["compare", "--side", "buy", "--qty", "1", "--duration", "6", "--slices", "3"],
        "record": ["record", "--duration", "5", "--out", "x.jsonl"],
    }[command]
    assert build_parser().parse_args([*base, "--venues", "kraken,coinbase"]).venues == (
        "kraken",
        "coinbase",
    )


class _FakeClient:
    def __init__(self, address: str) -> None:
        self.closed = False

    def wait_ready(self) -> None:
        pass

    def venue_fees(self) -> dict[str, float]:
        return {"kraken": 40.0}

    def close(self) -> None:
        self.closed = True


def test_venue_mismatch_with_engine_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SLIPSTREAM_PAPER_MODE", raising=False)
    monkeypatch.setattr("slipstream.cli.EngineClient", _FakeClient)
    assert main([*BASE, "--qty", "1", "--venues", "kraken,coinbase"]) == 1
    assert "do not match" in capsys.readouterr().err


def two_venue_status() -> pb.OrderStatus:
    return pb.OrderStatus(
        order_id="o-1",
        algo="twap",
        state=pb.ORDER_STATE_COMPLETED,
        total_qty=0.05,
        filled_qty=0.05,
        avg_fill_price=100019.0,
        arrival_mid=100002.5,
        slippage_bps=1.65,
        immediate_cost_bps=1.65,
        fees_paid=0.100015,
        fees_bps=0.2,
        routed_all_in_bps=1.85,
        venue_costs=[
            pb.VenueCost(venue="kraken", all_in_bps=1.95, available=True),
            pb.VenueCost(venue="coinbase", all_in_bps=3.05, available=True),
        ],
    )


def test_routing_gain_is_best_available_venue_minus_routed() -> None:
    assert routing_gain_bps(two_venue_status()) == pytest.approx(0.10)


def test_routing_gain_ignores_unavailable_venues_and_is_none_without_any() -> None:
    status = two_venue_status()
    status.venue_costs[0].available = False
    assert routing_gain_bps(status) == pytest.approx(1.20)
    status.venue_costs[1].available = False
    assert routing_gain_bps(status) is None


def test_summary_shows_fees_all_in_venues_and_gain() -> None:
    text = format_summary(two_venue_status())
    assert "fees         0.20 bps (0.10 paid)" in text
    assert "all-in       1.85 bps" in text
    assert "  kraken     1.95 bps" in text
    assert "  coinbase   3.05 bps" in text
    assert "routing gain 0.10 bps" in text


def test_summary_marks_unavailable_venue_na() -> None:
    status = two_venue_status()
    status.venue_costs[1].available = False
    assert "  coinbase   n/a" in format_summary(status)


def test_single_venue_summary_has_no_venue_breakdown() -> None:
    status = pb.OrderStatus(
        order_id="o",
        venue_costs=[pb.VenueCost(venue="kraken", all_in_bps=1.0, available=True)],
    )
    text = format_summary(status)
    assert "fees" in text
    assert "routing gain" not in text
    assert "  kraken" not in text


def test_comparison_table_adds_venue_columns_for_two_venues() -> None:
    header, row = format_comparison([two_venue_status()], []).splitlines()
    assert header.split()[-11:] == [
        "fee",
        "bps",
        "all-in",
        "bps",
        "kraken",
        "bps",
        "coinbase",
        "bps",
        "gain",
        "bps",
        "fills",
    ]
    assert row.split()[-6:] == ["0.20", "1.85", "1.95", "3.05", "0.10", "0"]


def test_comparison_table_marks_unavailable_venue_na() -> None:
    status = two_venue_status()
    status.venue_costs[1].available = False
    row = format_comparison([status], []).splitlines()[1]
    assert row.split()[-4:] == ["1.95", "n/a", "0.10", "0"]


def test_float_noise_gain_never_prints_negative_zero() -> None:
    status = two_venue_status()
    status.routed_all_in_bps = status.venue_costs[0].all_in_bps + 1e-13
    assert "routing gain 0.00 bps" in format_summary(status)
    row = format_comparison([status], []).splitlines()[1]
    assert "-0.00" not in row


VENUE_FLAGS_BASE = ["venue-flags", "--venues", "kraken,coinbase", "--fees", "kraken=40,coinbase=60"]


def test_fees_parses_valid_list() -> None:
    args = build_parser().parse_args(VENUE_FLAGS_BASE)
    assert args.fees == {"kraken": 40.0, "coinbase": 60.0}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "kraken",
        "kraken=",
        "kraken=abc",
        "kraken=-1",
        "kraken=1001",
        "kraken=nan",
        "kraken=inf",
        "kraken=40,kraken=50",
        "binance=1",
        ",",
    ],
)
def test_fees_rejects_bad_entries(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["venue-flags", "--venues", "kraken", "--fees", value])


def test_venue_flags_requires_fees() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["venue-flags", "--venues", "kraken"])


def test_venue_flags_default_symbol() -> None:
    args = build_parser().parse_args(VENUE_FLAGS_BASE)
    assert args.symbol == "BTC/USD"


def test_venue_flags_prints_plain_decimal_engine_args(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(venues: object, symbol: str) -> dict[str, VenueRules]:
        assert symbol == "BTC/USD"
        return {
            "kraken": VenueRules(min_qty=0.00005, qty_step=1e-08, min_notional=0.5),
            "coinbase": VenueRules(min_qty=1e-08, qty_step=1e-08, min_notional=1.0),
        }

    monkeypatch.setattr("slipstream.cli.fetch_venue_rules", fake_fetch)
    assert main(VENUE_FLAGS_BASE) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "--venue",
        "kraken:fee_bps=40,min_qty=0.00005,qty_step=0.00000001,min_notional=0.5",
        "--venue",
        "coinbase:fee_bps=60,min_qty=0.00000001,qty_step=0.00000001,min_notional=1",
    ]
    for line in out:
        assert "e-" not in line
        assert "E" not in line


def test_venue_flags_zero_rules_print_as_bare_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(venues: object, symbol: str) -> dict[str, VenueRules]:
        return {"kraken": VenueRules(min_qty=0.0, qty_step=0.0, min_notional=0.0)}

    monkeypatch.setattr("slipstream.cli.fetch_venue_rules", fake_fetch)
    assert main(["venue-flags", "--venues", "kraken", "--fees", "kraken=0"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["--venue", "kraken:fee_bps=0,min_qty=0,qty_step=0,min_notional=0"]


def test_venue_flags_missing_fee_for_venue_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["venue-flags", "--venues", "kraken,coinbase", "--fees", "kraken=40"]) == 1
    assert "coinbase" in capsys.readouterr().err


def test_venue_flags_exits_1_on_venue_rules_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(venues: object, symbol: str) -> dict[str, VenueRules]:
        raise VenueRulesError("boom")

    monkeypatch.setattr("slipstream.cli.fetch_venue_rules", fake_fetch)
    assert main(VENUE_FLAGS_BASE) == 1
    assert "boom" in capsys.readouterr().err


def test_venue_flags_exits_1_on_os_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_fetch(venues: object, symbol: str) -> dict[str, VenueRules]:
        raise OSError("network down")

    monkeypatch.setattr("slipstream.cli.fetch_venue_rules", fake_fetch)
    assert main(VENUE_FLAGS_BASE) == 1
    assert "network down" in capsys.readouterr().err


def test_venue_flags_does_not_require_paper_mode_or_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLIPSTREAM_PAPER_MODE", "false")

    def fake_fetch(venues: object, symbol: str) -> dict[str, VenueRules]:
        return {"kraken": VenueRules(min_qty=0.0, qty_step=0.0, min_notional=0.0)}

    monkeypatch.setattr("slipstream.cli.fetch_venue_rules", fake_fetch)
    assert main(["venue-flags", "--venues", "kraken", "--fees", "kraken=1"]) == 0
