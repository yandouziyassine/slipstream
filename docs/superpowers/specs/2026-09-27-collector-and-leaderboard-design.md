# Hourly Collector, Results Database and Live Research Page — Design

Date: 2026-09-27
Status: Approved in conversation; written spec awaiting user review
Scope: hosted-service sub-projects **0**, the historical results database, and **1**, the live leaderboard. See `2026-09-27-hosted-service-overview.md`.

## 1. Goal

Build public, honest proof of what execution strategy and venue choice really cost:
- Every hour, run paper orders on the live Kraken and Coinbase order books.
- Store every result permanently in a database.
- Publish a research page built from that data.

Collection starts on the user's PC now and moves to the Oracle Always Free VM later. Everything is paper-only.

## 2. Decisions (user, 2026-09-27)

| Question | Decision |
|---|---|
| Hourly workload | Two orders per hour, **0.01 BTC and 0.25 BTC**, each over **10 min**, with all four algorithms. **Buy on even UTC hours and sell on odd ones.** |
| Page layout | The research dashboard in mockup v3: a 3-line plain-English explainer with the GitHub link, KPIs, a 30-day chart with 95% bands, fees versus slippage, recent runs, a "Use it yourself" section announcing the API and MCP server as *coming soon*, and a not-financial-advice note. |
| Page host | **GitHub Pages**, from a separate public repo `slipstream-live`. The collector pushes to it over SSH with a **deploy key limited to that repo**. No inbound ports anywhere. |
| Collection start | **Now, on the user's PC**, run by an hourly Windows scheduled task that calls WSL. The database is imported into the VM once the VM exists. |
| Raw recordings | Kept compressed. Older than 30 days, thinned to one recording per day. |

## 3. Collector

```
Windows Task Scheduler (hourly, runs as the user, not admin)
  └─ wsl bash /mnt/c/Code/slipstream/scripts/collect_hourly.sh
        1. acquire $DATA/collect.lock (non-blocking); if held → log "skipped: previous run active", exit 0
        2. build (incremental) and start the release engine:
             --clock live, venue flags from `slipstream venue-flags`
             paper limits: --max-order-notional 50000 --max-position 2
        3. python -m slipstream.collect run
             for size in (0.01, 0.25): LiveSession with all four algorithms, side from the UTC hour,
             duration 600 s, 10 slices (one per minute) for TWAP/VWAP/AC, POV participation 0.1
             record the run's raw market data alongside (existing recorder, raw text, gzip)
        4. write results to $DATA/slipstream.db
        5. python -m slipstream.site build && publish   (step 1; a publish failure is logged, never fatal)
        6. stop the engine, release the lock
```

- **The two sizes run one after the other,** each with its own engine session. Together they take about 20–22 minutes, well inside the hour.
- **A run that fails is recorded with `status = failed` and its error message.** A crash leaves the run marked `started` with no end time, and the next run records it as `abandoned`. No failure is hidden.
- **Venue rules and fees are captured on every run,** so the conditions of each run are fully documented.

## 4. Database (`$DATA/slipstream.db`)

**Data directory:** `$DATA` is `SLIPSTREAM_DATA_DIR`, and defaults to `~/slipstream-data` on the WSL Linux filesystem, never under `/mnt/c`. SQLite's WAL locking and shared memory are unreliable on the 9P mount WSL uses for Windows drives, and the native filesystem is also much faster. The database, raw recordings, backups, logs, `site/` output and the publish switch all live there, outside the repo.

SQLite in WAL mode. Every connection runs `PRAGMA foreign_keys=ON`. Migrations are fixed SQL files under `python/slipstream/db/migrations/`, and the `schema_version` table records which have run.

| Table | Columns (main) |
|---|---|
| `runs` | `id`, `started_at`, `ended_at`, `status` (started/completed/failed/abandoned), `side`, `qty`, `duration_s`, `fees_json`, `venue_rules_json`, `git_commit`, `error`, `supersedes_id` |
| `recordings` | `run_id`, `path`, `sha256`, `recorded_at` |
| `recording_deletions` | `run_id`, `deleted_at` (thinning is recorded as a deletion row, never by editing `recordings`) |
| `results` | `run_id`, `algo`, `state`, `filled_qty`, `filled_pct`, `avg_price`, `arrival_mid`, `slippage_bps`, `fee_bps`, `all_in_bps`, `immediate_cost_bps`, `routing_gain_bps` (nullable), `venue_costs_json`, `fills_count`, `halt_reason` |
| `fills` | `run_id`, `algo`, `venue`, `qty`, `price`, `fee`, `ts_ns` |
| `engine_stats` | `run_id`, `latency_p50_ns`, `latency_p99_ns`, `events` |
| `schema_version` | `version`, `applied_at` |

- **Rows are append-only.** `UPDATE` and `DELETE` triggers on every table abort. The only exception is closing a run: a trigger allows a single `started → completed/failed/abandoned` transition, setting `ended_at` and `error` once and nothing else.
- **A correction is a new run** with `supersedes_id` pointing at the old one.
- **Every query uses parameters.**
- **Backups:** after each run the collector writes an online backup (SQLite backup API) to `$DATA/backup/slipstream-<date>.db`, keeping 14 days. Oracle Object Storage is added in the VM phase.
- **Thinning:** once a day, raw recordings older than 30 days are thinned, keeping one per UTC day (the 12:00 run). Each deleted file gets a `recording_deletions` row, and its `recordings` row with the SHA-256 is kept, so the fact that it existed stays auditable.

## 5. Site (`python -m slipstream.site`)

- **`build`** writes `site/` from the database, deterministically for a given database:
  - `index.html`: the research page.
  - `history.html`: every run, filterable client-side with a small inline script and no external library.
  - `methodology.html`: how runs work, the fees, the known limits, and a link to the source.
  - `data/runs.csv`: the flat results export.
  - `assets/`: fonts stored in the repo, and CSS.
- **Charts** are server-rendered SVG from Python. There is no charting library and no third-party script, font or analytics. There are no cookies.
- **Statistics:**
  - Per algorithm and size: the median all-in cost with a 95% bootstrap interval (1,000 resamples, fixed seed).
  - "Strategy X is cheaper" is shown only when the difference's 95% interval excludes 0. Otherwise the page says "not yet distinguishable (n = …)".
  - Every figure shows its n.
- **Honesty text** is always present:
  - paper trading on real public prices;
  - fills do not consume liquidity;
  - fees are entry-tier examples;
  - not financial advice.
- **Escaping:** every dynamic value is HTML-escaped by one helper. There is a test for a hostile string.
- **`publish`:**
  - Commits `site/` into a local clone of `slipstream-live` and pushes over SSH. The deploy key lives at `~/.ssh/slipstream_live_deploy` (mode 600) and git uses it through `GIT_SSH_COMMAND` with `IdentitiesOnly=yes`.
  - The commit author is the noreply address.
  - Publishing is enabled only when `$DATA/publish.enabled` exists. The user creates that file after approving, so nothing is published by accident.

## 6. Security

- **Paper only:** there is no code path that places orders.
- **Outbound only:** exchange WebSockets over verified TLS, and git over SSH to one repo. The collector has no listening socket, and the engine binds to loopback only.
- **Least privilege:**
  - The scheduled task runs as the user with no stored password, only while the user is logged on, and calls one fixed script with fixed arguments.
  - The deploy key can write to `slipstream-live` only.
- **Secrets:**
  - The deploy key never enters any repo, log or output.
  - All data and site output lives in `$DATA`, outside the repo, so it can never be committed.
- **Input handling:** everything from the exchanges goes through the existing validating parsers. Database values rendered to HTML are escaped.

## 7. Testing

- **Database:**
  - Migrations apply from an empty file.
  - The triggers block `UPDATE` and `DELETE` apart from the allowed run closing.
  - A failed run is persisted.
  - An abandoned run is detected.
  - The lock prevents a second concurrent run.
  - Thinning keeps the 12:00 recording.
- **Collector:** using the existing fake Kraken and Coinbase servers and a real live-mode engine, both sizes and all four algorithms produce the expected rows, fills and stats. An exchange error produces a failed run row.
- **Site:**
  - An HTML snapshot from a fixed test database.
  - The bootstrap interval checked against a hand-computed case.
  - The "not yet distinguishable" rule tested both ways.
  - The escaping test.
  - Publish is disabled without `$DATA/publish.enabled`, and runs against a local bare git repo in tests (no network).
- **Manual rollout:**
  1. One real hourly run on the user's PC.
  2. A local preview of `site/`, which the user checks.
  3. The first publish, only after the user creates the repo and approves.

## 8. Delivery

Both PRs come after pipeline PR 3:

1. **Collector and database.** Adds `slipstream/db/`, `slipstream/collect.py`, `scripts/collect_hourly.sh`, and `scripts/install_task.ps1`, which registers the Windows scheduled task. The user approves running it, and it can also remove the task.
2. **Site generator and publishing.** Adds `slipstream/site/` with its templates and fonts, and the publish step. `input.md` lists the user steps: create `slipstream-live`, enable Pages, add the deploy key, then create `$DATA/publish.enabled`.

## 9. Out of scope

- The Oracle VM setup, which gets its own spec when the account exists.
- The public API and MCP server (sub-projects 2 and 3).
- Additional venues.
- Liquidity-consuming fills (Phase 2 sub-project 4). Until then the methodology page states the limitation.
