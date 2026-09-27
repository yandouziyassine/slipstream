from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slipstream.db import ResultsDB
from slipstream.models import Fill
from slipstream.site.render import (
    GITHUB_URL,
    build_history_html,
    build_index_html,
    build_methodology_html,
    build_runs_csv,
    write_site,
)
from slipstream.v1 import execution_pb2 as pb
from slipstream.venue_rules import VenueRules

FEES = {"kraken": 40.0, "coinbase": 60.0}
RULES = {
    "kraken": VenueRules(min_qty=0.00005, qty_step=1e-8, min_notional=0.5),
    "coinbase": VenueRules(min_qty=1e-8, qty_step=1e-8, min_notional=1.0),
}
ALGOS = ("twap", "vwap", "pov", "almgren_chriss")
FIXED_NOW = datetime(2026, 9, 27, 15, 0, 0, tzinfo=UTC)
SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "index_snapshot.html"


def _status(algo: str, all_in_bps: float, order_id: str, halt_reason: str = "") -> pb.OrderStatus:
    return pb.OrderStatus(
        order_id=order_id,
        algo=algo,
        state=pb.ORDER_STATE_COMPLETED,
        total_qty=0.01,
        filled_qty=0.01,
        avg_fill_price=100000.0,
        arrival_mid=100000.0,
        slippage_bps=all_in_bps - 0.4,
        immediate_cost_bps=all_in_bps,
        fees_bps=0.4,
        routed_all_in_bps=all_in_bps,
        halt_reason=halt_reason,
        venue_costs=[
            pb.VenueCost(venue="kraken", all_in_bps=all_in_bps + 0.1, available=True),
            pb.VenueCost(venue="coinbase", all_in_bps=all_in_bps + 0.3, available=True),
        ],
    )


def _stats() -> pb.EngineStats:
    return pb.EngineStats(events=10, latency_p50_ns=38000, latency_p99_ns=210000)


def _fill(order_id: str, venue: str = "kraken") -> Fill:
    return Fill(order_id=order_id, ts_ns=1, qty=0.005, price=100000.0, venue=venue, fee=0.02)


@pytest.fixture
def snapshot_db(tmp_path: Path) -> ResultsDB:
    instance = ResultsDB(tmp_path / "slipstream.db")
    instance.migrate()
    started = datetime(2026, 9, 27, 14, 0, 0, tzinfo=UTC)
    all_in = {"twap": 40.35, "vwap": 40.35, "pov": 40.20, "almgren_chriss": 40.33}
    run_id = instance.begin_run(started, "buy", 0.01, 600, FEES, RULES, "abc123")
    statuses = [_status(algo, all_in[algo], f"h1-{algo}") for algo in ALGOS]
    fills = [_fill(f"h1-{algo}") for algo in ALGOS]
    instance.add_results(run_id, statuses, fills, _stats())
    instance.finish_run(run_id, "completed", started)
    return instance


def test_build_index_html_matches_snapshot(snapshot_db: ResultsDB) -> None:
    html = build_index_html(snapshot_db.connection, FIXED_NOW)
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert html == expected


def test_build_index_html_is_deterministic(snapshot_db: ResultsDB) -> None:
    first = build_index_html(snapshot_db.connection, FIXED_NOW)
    second = build_index_html(snapshot_db.connection, FIXED_NOW)
    assert first == second


@pytest.fixture
def hostile_db(tmp_path: Path) -> ResultsDB:
    instance = ResultsDB(tmp_path / "slipstream.db")
    instance.migrate()
    started = datetime(2026, 9, 27, 14, 0, 0, tzinfo=UTC)
    hostile = "<script>alert(1)</script>"
    ok_run = instance.begin_run(started, "buy", 0.01, 600, FEES, RULES, "abc123")
    statuses = [_status("twap", 40.3, "h2-twap", halt_reason=hostile)]
    instance.add_results(ok_run, statuses, [_fill("h2-twap")], _stats())
    instance.finish_run(ok_run, "completed", started)

    failed_run = instance.begin_run(started, "sell", 0.25, 600, FEES, RULES, "abc123")
    instance.finish_run(failed_run, "failed", started, error=hostile)
    return instance


def test_hostile_halt_reason_is_escaped_in_csv(hostile_db: ResultsDB) -> None:
    csv_text = build_runs_csv(hostile_db.connection)
    assert "<script>alert(1)</script>" in csv_text  # CSV is raw data, not HTML
    assert "<script>alert(1)</script>" not in build_index_html(hostile_db.connection, FIXED_NOW)
    assert "<script>alert(1)</script>" not in build_history_html(hostile_db.connection)


def test_hostile_run_error_is_escaped_in_history(hostile_db: ResultsDB) -> None:
    history = build_history_html(hostile_db.connection)
    assert "<script>alert(1)</script>" not in history
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in history


def test_runs_csv_defuses_a_leading_equals_sign(tmp_path: Path) -> None:
    instance = ResultsDB(tmp_path / "slipstream.db")
    instance.migrate()
    started = datetime(2026, 9, 27, 14, 0, 0, tzinfo=UTC)
    run_id = instance.begin_run(started, "buy", 0.01, 600, FEES, RULES, None)
    instance.finish_run(run_id, "failed", started, error="=cmd|'/c calc'!A1")

    csv_text = build_runs_csv(instance.connection)

    assert "\n=cmd" not in csv_text
    assert "'=cmd" in csv_text


@pytest.mark.parametrize(
    "build",
    [
        lambda conn: build_index_html(conn, FIXED_NOW),
        build_history_html,
        lambda conn: build_methodology_html(),
    ],
)
def test_no_external_url_except_github(hostile_db: ResultsDB, build: object) -> None:
    html = build(hostile_db.connection)  # type: ignore[operator]
    urls = re.findall(r"https?://[^\s\"'<>]+", html)
    assert urls
    assert all(url == GITHUB_URL or url.startswith(GITHUB_URL) for url in urls)


def test_write_site_creates_the_expected_files(snapshot_db: ResultsDB, tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    write_site(snapshot_db.connection, out_dir, FIXED_NOW)

    assert (out_dir / "index.html").exists()
    assert (out_dir / "history.html").exists()
    assert (out_dir / "methodology.html").exists()
    assert (out_dir / "data" / "runs.csv").exists()


def test_build_methodology_html_mentions_known_limits() -> None:
    html = build_methodology_html()
    assert "liquidity" in html
    assert GITHUB_URL in html
