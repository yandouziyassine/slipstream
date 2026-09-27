from __future__ import annotations

import csv
import io
import json
import sqlite3
import string
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from pathlib import Path

from slipstream.site.escaping import esc
from slipstream.site.stats import median_ci
from slipstream.site.svg import bar_split, line_chart

GITHUB_URL = "https://github.com/yandouziyassine/slipstream"
ALGOS: tuple[str, ...] = ("twap", "vwap", "pov", "almgren_chriss")
ALGO_LABELS: dict[str, str] = {
    "twap": "TWAP",
    "vwap": "VWAP",
    "pov": "POV",
    "almgren_chriss": "A-C",
}
_CHART_WINDOW_DAYS = 30
_FEE_SPLIT_WINDOW_DAYS = 7
_RUNS_TABLE_SIZE = 8

_CSS = """
:root {
  --paper:#f6f4ee; --ink:#1b1b1a; --muted:#6b6a64; --rule:#d9d5c9;
  --good:#1f6b47; --bad:#a3401f; --accent:#1f4e79;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width:1040px; margin:0 auto; padding:28px 28px 60px; }
header {
  display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:2px solid var(--ink); padding-bottom:10px;
}
.brand { font:600 20px Georgia, Charter, serif; letter-spacing:-.01em; }
.muted { color:var(--muted); font-weight:400; }
nav a { color:var(--ink); text-decoration:none; margin-left:18px; font-size:14px; }
nav a:hover { text-decoration:underline; }
.intro { display:grid; grid-template-columns:1.5fr 1fr; gap:36px; margin:22px 0 26px; }
.intro p { margin:0 0 6px; font:18px/1.5 Georgia, Charter, serif; }
.gh { font-size:14px; margin-top:10px; }
.gh a, .follow a, .note a { color:var(--accent); }
.status {
  border-left:1px solid var(--rule); padding-left:20px;
  font-size:13px; color:var(--muted);
}
.status b { color:var(--ink); font-weight:500; }
.unit-note { margin-top:8px; }
.mono, td.num, .kpi .v {
  font-family:ui-monospace, Consolas, "SF Mono", monospace;
  font-variant-numeric:tabular-nums;
}
.kpis {
  display:grid; grid-template-columns:repeat(4,1fr);
  border-top:1px solid var(--rule); border-bottom:1px solid var(--rule);
}
.kpis.two-col { grid-template-columns:1fr 1fr; border-top:0; }
.kpi { padding:14px 16px; border-right:1px solid var(--rule); }
.kpi:last-child { border-right:0; }
.kpi .l {
  font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em;
}
.kpi .v { font-size:24px; margin-top:4px; font-family:ui-monospace, Consolas, monospace; }
.kpi .n { font-size:12px; color:var(--muted); }
.kpi .body { margin:6px 0 0; font-size:14px; }
h2 { font:600 17px Georgia, Charter, serif; margin:30px 0 4px; }
.sub { color:var(--muted); font-size:13px; margin:0 0 12px; }
.grid2 { display:grid; grid-template-columns:1.4fr 1fr; gap:32px; }
svg text { font:11px ui-monospace, Consolas, monospace; fill:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:13.5px; }
th {
  text-align:left; font-weight:500; color:var(--muted);
  border-bottom:1px solid var(--ink); padding:6px 8px; font-size:12px;
}
td { padding:6px 8px; border-bottom:1px solid var(--rule); }
td.num, th.num {
  text-align:right; font-family:ui-monospace, Consolas, "SF Mono", monospace;
}
.win { color:var(--good); font-weight:500; }
.tag { font-size:11px; border:1px solid var(--rule); padding:1px 6px; color:var(--muted); }
.note {
  font-size:13px; color:var(--muted); border-top:1px solid var(--rule);
  margin-top:34px; padding-top:12px;
  display:grid; grid-template-columns:1fr 1fr; gap:24px;
}
.note b { color:var(--ink); font-weight:500; }
.follow { margin-top:10px; }
"""


@dataclass(frozen=True)
class RunRow:
    id: int
    started_at: datetime
    ended_at: datetime | None
    status: str
    side: str
    qty: float
    duration_s: int
    fees_json: str
    error: str | None


@dataclass(frozen=True)
class ResultRow:
    algo: str
    all_in_bps: float
    slippage_bps: float
    fee_bps: float
    routing_gain_bps: float | None
    halt_reason: str | None
    filled_pct: float


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def _load_template(name: str) -> string.Template:
    path = resources.files("slipstream.site").joinpath("templates", name)
    return string.Template(path.read_text(encoding="utf-8"))


def _row_to_run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        started_at=_parse_iso(row["started_at"]),
        ended_at=_parse_iso(row["ended_at"]) if row["ended_at"] else None,
        status=row["status"],
        side=row["side"],
        qty=row["qty"],
        duration_s=row["duration_s"],
        fees_json=row["fees_json"],
        error=row["error"],
    )


def _query_runs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    qty: float | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[RunRow]:
    conn.row_factory = sqlite3.Row
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if qty is not None:
        clauses.append("qty = ?")
        params.append(qty)
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since.strftime("%Y-%m-%dT%H:%M:%S"))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM runs{where} ORDER BY started_at DESC"  # noqa: S608 - fixed columns/table
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_run(row) for row in rows]


def _results_for_run(conn: sqlite3.Connection, run_id: int) -> list[ResultRow]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT algo, all_in_bps, slippage_bps, fee_bps, routing_gain_bps, halt_reason, filled_pct "
        "FROM results WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [
        ResultRow(
            algo=row["algo"],
            all_in_bps=row["all_in_bps"],
            slippage_bps=row["slippage_bps"],
            fee_bps=row["fee_bps"],
            routing_gain_bps=row["routing_gain_bps"],
            halt_reason=row["halt_reason"],
            filled_pct=row["filled_pct"],
        )
        for row in rows
    ]


def _fill_venue_split(conn: sqlite3.Connection, run_id: int) -> dict[str, float]:
    rows = conn.execute(
        "SELECT venue, SUM(qty) FROM fills WHERE run_id = ? GROUP BY venue", (run_id,)
    ).fetchall()
    total = sum(qty for _, qty in rows)
    if total <= 0:
        return {}
    return {venue: qty / total * 100.0 for venue, qty in rows}


def _distinct_qtys(conn: sqlite3.Connection) -> list[float]:
    rows = conn.execute("SELECT DISTINCT qty FROM runs ORDER BY qty ASC").fetchall()
    return [row[0] for row in rows]


def _avg_engine_stats(conn: sqlite3.Connection) -> tuple[float, float] | None:
    row = conn.execute(
        "SELECT AVG(latency_p50_ns), AVG(latency_p99_ns) FROM engine_stats"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0] / 1000.0, row[1] / 1000.0


def _avg_routing_gain(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT AVG(res.routing_gain_bps) FROM results res "
        "JOIN runs r ON r.id = res.run_id "
        "WHERE r.status = 'completed' AND res.routing_gain_bps IS NOT NULL"
    ).fetchone()
    return None if row is None else row[0]


def _fees_summary(fees_json: str) -> str:
    fees = json.loads(fees_json)
    parts = [f"{venue.capitalize()} {bps:g} bps" for venue, bps in sorted(fees.items())]
    return " · ".join(parts) + " (entry tier)" if parts else "n/a"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{round(value, digits) + 0.0:.{digits}f}"


def _status_block(conn: sqlite3.Connection) -> dict[str, str]:
    runs = _query_runs(conn, limit=1)
    if not runs:
        return {
            "last_run": "no runs yet",
            "next_run": "n/a",
            "feeds": "n/a",
            "fees_used": "n/a",
        }
    last = runs[0]
    next_run = last.started_at + timedelta(hours=1)
    feeds = "both recorded this run" if last.status == "completed" else f"last run {last.status}"
    return {
        "last_run": f"{last.started_at:%H:%M} UTC · {last.side} {last.qty:g} BTC over "
        f"{last.duration_s // 60} min",
        "next_run": f"{next_run:%H:%M} UTC",
        "feeds": feeds,
        "fees_used": _fees_summary(last.fees_json),
    }


def _kpi_block(conn: sqlite3.Connection, now: datetime) -> dict[str, str]:
    recent = _query_runs(conn, status="completed", limit=1)
    cheapest_algo, cheapest_detail = "n/a", "no completed runs yet"
    if recent:
        results = _results_for_run(conn, recent[0].id)
        if results:
            best = min(results, key=lambda r: r.all_in_bps)
            cheapest_algo = ALGO_LABELS.get(best.algo, best.algo)
            cheapest_detail = f"{_num(best.all_in_bps)} bps all-in"

    all_runs = _query_runs(conn)
    runs_count = str(len(all_runs))
    runs_since = all_runs[-1].started_at.strftime("%Y-%m-%d") if all_runs else "n/a"

    routing_gain = _num(_avg_routing_gain(conn))
    stats = _avg_engine_stats(conn)
    latency_p50 = f"{stats[0]:.0f} µs" if stats else "n/a"
    latency_p99 = f"{stats[1]:.0f} µs" if stats else "n/a"

    return {
        "kpi_cheapest_algo": cheapest_algo,
        "kpi_cheapest_detail": cheapest_detail,
        "kpi_runs_count": runs_count,
        "kpi_runs_since": runs_since,
        "kpi_routing_gain": f"{routing_gain} bps",
        "kpi_latency_p50": latency_p50,
        "kpi_latency_p99": latency_p99,
    }


def _daily_medians(
    conn: sqlite3.Connection, qty: float, algo: str, since: datetime
) -> list[tuple[date, float, float, float]]:
    rows = conn.execute(
        "SELECT r.started_at, res.all_in_bps FROM runs r JOIN results res ON res.run_id = r.id "
        "WHERE r.status = 'completed' AND r.qty = ? AND res.algo = ? AND r.started_at >= ?",
        (qty, algo, since.strftime("%Y-%m-%dT%H:%M:%S")),
    ).fetchall()
    by_day: dict[date, list[float]] = {}
    for started_at, all_in_bps in rows:
        day = _parse_iso(started_at).date()
        by_day.setdefault(day, []).append(all_in_bps)
    out: list[tuple[date, float, float, float]] = []
    for day in sorted(by_day):
        median, lo, hi = median_ci(by_day[day])
        out.append((day, median, lo, hi))
    return out


def _chart_for_size(conn: sqlite3.Connection, qty: float, now: datetime) -> str:
    since = now - timedelta(days=_CHART_WINDOW_DAYS)
    series: dict[str, list[tuple[date, float]]] = {}
    bands: dict[str, list[tuple[date, float, float]]] = {}
    best_algo, best_last = None, None
    for algo in ALGOS:
        daily = _daily_medians(conn, qty, algo, since)
        if not daily:
            continue
        label = ALGO_LABELS[algo]
        series[label] = [(day, median) for day, median, _, _ in daily]
        bands_for_algo = [(day, lo, hi) for day, _, lo, hi in daily]
        last_median = daily[-1][1]
        if best_last is None or last_median < best_last:
            best_algo, best_last = label, last_median
        bands[label] = bands_for_algo
    if best_algo is not None:
        bands = {best_algo: bands[best_algo]}
    else:
        bands = {}
    return line_chart(series, bands)


def _fee_split_rows(conn: sqlite3.Connection, now: datetime) -> list[tuple[str, float, float]]:
    since = now - timedelta(days=_FEE_SPLIT_WINDOW_DAYS)
    rows: list[tuple[str, float, float]] = []
    for algo in ALGOS:
        result = conn.execute(
            "SELECT AVG(res.fee_bps), AVG(res.slippage_bps) FROM results res "
            "JOIN runs r ON r.id = res.run_id "
            "WHERE r.status = 'completed' AND res.algo = ? AND r.started_at >= ?",
            (algo, since.strftime("%Y-%m-%dT%H:%M:%S")),
        ).fetchone()
        if result is None or result[0] is None:
            continue
        rows.append((ALGO_LABELS[algo], result[0], result[1]))
    return rows


def _charts_html(conn: sqlite3.Connection, now: datetime) -> str:
    qtys = _distinct_qtys(conn)
    chart_blocks = []
    for qty in qtys:
        chart_blocks.append(
            f"<div><h2>All-in cost by strategy, {qty:g} BTC, last {_CHART_WINDOW_DAYS} days</h2>"
            f'<p class="sub">Daily median, basis points. Lower is cheaper. Shaded band is the '
            f"95% range for the day's cheapest strategy.</p>"
            f"{_chart_for_size(conn, qty, now)}</div>"
        )
    fee_rows = _fee_split_rows(conn, now)
    fee_chart = (
        f"<div><h2>Where the cost comes from</h2>"
        f'<p class="sub">Average fee vs. slippage, last {_FEE_SPLIT_WINDOW_DAYS} days. '
        f"Black = exchange fees · rust = slippage.</p>"
        f"{bar_split(fee_rows)}</div>"
    )
    return f'<div class="grid2">{"".join(chart_blocks)}{fee_chart}</div>'


def _runs_table_html(conn: sqlite3.Connection) -> str:
    qtys = _distinct_qtys(conn)
    table_qty = qtys[0] if qtys else None
    runs = _query_runs(conn, status="completed", qty=table_qty, limit=_RUNS_TABLE_SIZE)
    if not runs:
        return '<p class="sub">No completed runs yet.</p>'
    header_cells = "".join(f'<th class="num">{esc(ALGO_LABELS[algo])}</th>' for algo in ALGOS)
    rows_html = []
    for run in runs:
        results = {r.algo: r for r in _results_for_run(conn, run.id)}
        cheapest = min(results.values(), key=lambda r: r.all_in_bps) if results else None
        cells = []
        for algo in ALGOS:
            result = results.get(algo)
            if result is None:
                cells.append('<td class="num">n/a</td>')
                continue
            is_win = cheapest is not None and result.algo == cheapest.algo
            cls = "num win" if is_win else "num"
            cells.append(f'<td class="{cls}">{esc(_num(result.all_in_bps))}</td>')
        venues = _fill_venue_split(conn, run.id)
        kraken_pct, coinbase_pct = venues.get("kraken", 0.0), venues.get("coinbase", 0.0)
        venue_split = " / ".join(esc(round(pct)) for pct in (kraken_pct, coinbase_pct))
        gains = [r.routing_gain_bps for r in results.values() if r.routing_gain_bps is not None]
        gain = sum(gains) / len(gains) if gains else None
        cheapest_label = ALGO_LABELS.get(cheapest.algo, "n/a") if cheapest else "n/a"
        rows_html.append(
            "<tr>"
            f'<td class="mono">{esc(run.started_at.strftime("%H:%M"))}</td>'
            f'<td class="win">{esc(cheapest_label)}</td>'
            f"{''.join(cells)}"
            f'<td class="num">{venue_split}</td>'
            f'<td class="num">{esc(_num(gain))}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Time (UTC)</th><th>Cheapest</th>"
        f'{header_cells}<th class="num">Kraken / Coinbase</th><th class="num">Routing gain</th>'
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )


def build_index_html(conn: sqlite3.Connection, now: datetime) -> str:
    content_tmpl = _load_template("index.html")
    content = content_tmpl.substitute(
        github_url=esc(GITHUB_URL),
        github_label=esc("github.com/yandouziyassine/slipstream"),
        runs_table_count=esc(_RUNS_TABLE_SIZE),
        charts=_charts_html(conn, now),
        runs_table=_runs_table_html(conn),
        **{k: esc(v) for k, v in _status_block(conn).items()},
        **{k: esc(v) for k, v in _kpi_block(conn, now).items()},
    )
    base_tmpl = _load_template("base.html")
    return base_tmpl.substitute(
        title="Slipstream — live execution costs",
        css=_CSS,
        github_url=esc(GITHUB_URL),
        content=content,
    )


def _history_rows_html(conn: sqlite3.Connection) -> tuple[str, int]:
    runs = _query_runs(conn)
    rows = []
    for run in runs:
        rows.append(
            f'<tr data-side="{esc(run.side)}" data-status="{esc(run.status)}">'
            f'<td class="mono">{esc(run.started_at.strftime("%Y-%m-%d %H:%M"))}</td>'
            f"<td>{esc(run.status)}</td>"
            f"<td>{esc(run.side)}</td>"
            f'<td class="num">{esc(run.qty)}</td>'
            f'<td class="num">{esc(run.duration_s)}</td>'
            f"<td>{esc(run.error or '')}</td>"
            "</tr>"
        )
    return "\n".join(rows), len(runs)


def build_history_html(conn: sqlite3.Connection) -> str:
    rows, count = _history_rows_html(conn)
    content_tmpl = _load_template("history.html")
    content = content_tmpl.substitute(rows=rows, rows_count=esc(count))
    base_tmpl = _load_template("base.html")
    return base_tmpl.substitute(
        title="Slipstream — run history",
        css=_CSS,
        github_url=esc(GITHUB_URL),
        content=content,
    )


def build_methodology_html() -> str:
    content_tmpl = _load_template("methodology.html")
    content = content_tmpl.substitute(
        github_url=esc(GITHUB_URL), github_label=esc("github.com/yandouziyassine/slipstream")
    )
    base_tmpl = _load_template("base.html")
    return base_tmpl.substitute(
        title="Slipstream — methodology",
        css=_CSS,
        github_url=esc(GITHUB_URL),
        content=content,
    )


_CSV_HOSTILE_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value: str) -> str:
    return f"'{value}" if value and value[0] in _CSV_HOSTILE_PREFIXES else value


def build_runs_csv(conn: sqlite3.Connection) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "run_id",
            "started_at",
            "status",
            "side",
            "qty",
            "algo",
            "state",
            "filled_pct",
            "all_in_bps",
            "slippage_bps",
            "fee_bps",
            "routing_gain_bps",
            "halt_reason",
            "error",
        ]
    )
    for run in _query_runs(conn):
        results = _results_for_run(conn, run.id)
        if not results:
            writer.writerow(
                [
                    run.id,
                    run.started_at.isoformat(),
                    run.status,
                    run.side,
                    run.qty,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    _csv_safe(run.error or ""),
                ]
            )
            continue
        for result in results:
            writer.writerow(
                [
                    run.id,
                    run.started_at.isoformat(),
                    run.status,
                    run.side,
                    run.qty,
                    result.algo,
                    "",
                    result.filled_pct,
                    result.all_in_bps,
                    result.slippage_bps,
                    result.fee_bps,
                    "" if result.routing_gain_bps is None else result.routing_gain_bps,
                    _csv_safe(result.halt_reason or ""),
                    _csv_safe(run.error or ""),
                ]
            )
    return buffer.getvalue()


def write_site(conn: sqlite3.Connection, out_dir: Path, now: datetime) -> None:
    """Writes site/ deterministically for the given database and timestamp."""
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(build_index_html(conn, now), encoding="utf-8")
    (out_dir / "history.html").write_text(build_history_html(conn), encoding="utf-8")
    (out_dir / "methodology.html").write_text(build_methodology_html(), encoding="utf-8")
    (out_dir / "data" / "runs.csv").write_text(build_runs_csv(conn), encoding="utf-8")
