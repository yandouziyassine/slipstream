# Slipstream

Open-source smart order execution engine. Slipstream slices a large order over time (TWAP), checks every slice against hard risk limits in C++ before it fills, and reports the slippage it paid against what a single market order would have cost at arrival.

> **Status: in development. Paper trading only.** Slipstream streams the public BTC/USD order books of Kraken and Coinbase and routes each slice across both venues. It uses no API keys, places no real orders, and moves no real money.

## Why

A large market order moves the price against the trader who sends it (market impact). Institutional desks pay for execution algorithms and smart order routers to reduce that cost. Retail traders and small funds usually send one market order and absorb the slippage. Slipstream brings the same approach into the open, where anyone can audit and measure it.

## How it works

```
Kraken WS v2 (public) ──► feed process ──┐
                                          │  MarketStream (gRPC, loopback)
Coinbase WS (public) ───► feed process ──┘  (one per venue)
                          validate + parse
                                             ▼
                               single-writer engine loop
                               one book per venue
                               schedule (TWAP/VWAP/POV/AC)
                               smart router (price + fee)
                               risk gate ──► simulated fills
                                             │
                                             │  Subscribe (fills + order updates)
      main process  ◄────────────────────────┘
      JSON logs + summary
```

Each venue's WebSocket feed runs in its own OS process, so a slow Coinbase snapshot never delays a Kraken price. Each feed process opens one long-lived `MarketStream` to the engine; a single engine thread reads every venue's stream plus one `Subscribe` stream carrying fills and order updates back to the main process, so all state (books, orders, risk) is touched by exactly one thread with no locking on the hot path.

**Clock modes** (`--clock live|replay`, engine flag):
- `live` (the default): the engine stamps every event with its own clock and ignores any client-supplied time, so a client cannot spoof it. A 50 ms timer also ticks the engine when the market is quiet, so schedules still advance.
- `replay`: the caller's `recv_ns` on each event drives the clock; time must never go backwards and there is no timer. This is what tests and the benchmark use, so results are exact and reproducible.

- **Smart order routing across venues.** Each child slice is split across Kraken and Coinbase by all-in price, meaning price plus that venue's taker fee. At the same moment the engine also prices the same quantity on each venue alone, so every run reports what routing actually saved.

- **Four execution schedules** behind one C++ `Schedule` interface:
  - **TWAP:** equal slices over time.
  - **VWAP:** follows the historical time-of-day volume profile, built from Kraken 15-minute candles.
  - **POV:** trades a fixed share of live market volume.
  - **Almgren-Chriss:** the closed-form optimal trade-off between market impact and price risk. It is calibrated live: σ from 1-minute candles, η from the order book's depth curve, and λ from an urgency preset.
- **Deterministic engine.** The caller supplies the clock (`now_ns`), so live runs, replays, and tests all exercise one code path.
- **Risk before every fill.** The engine enforces a per-order notional budget (slippage included) and a maximum absolute position. A breach halts the order; it never skips the check.
- **Measured outcome.** Each order reports its average fill price and its slippage against the arrival mid. It also reports the cost one market order for the full size would have paid at arrival (the "one-shot" benchmark).

## Tech stack

| Layer | Technology |
|---|---|
| Engine | C++20, gRPC 1.51, Protocol Buffers, CMake + Ninja, GoogleTest, ASan + UBSan |
| Orchestrator | Python 3.12, grpcio, websockets, pytest, `mypy --strict`, ruff |
| Contract | `proto/slipstream/v1/execution.proto` |
| Tooling | WSL2 Ubuntu 24.04, hash-pinned dependencies, pip-audit, GitHub Actions (`ubuntu-24.04` runners) |

## Repository layout

```
engine/            C++ engine: order book, TWAP, risk, fill simulator, gRPC service
python/slipstream  Python orchestrator: Kraken parser, engine client, runner, replay, live feed, CLI
proto/             gRPC contract shared by both sides
scripts/           build, codegen, and demo scripts
docs/superpowers/  design spec and implementation plans
```

## Build and test (WSL2 Ubuntu 24.04, or native Ubuntu 24.04)

One-time setup: `bash scripts/setup_wsl.sh` (installs the apt toolchain and creates a venv at `~/.venvs/slipstream` with hash-verified dependencies). Then, with the venv on `PATH`:

```bash
export PATH="$HOME/.venvs/slipstream/bin:$PATH"

bash scripts/gen_proto.sh
bash scripts/build_engine.sh
ctest --test-dir build/engine --output-on-failure

(cd python && pytest -q)
```

## Run a paper execution

Replay a recorded order book:

```bash
build/engine/slipstream_engine --listen 127.0.0.1:50051 --max-order-notional 10000 &
PYTHONPATH=python ~/.venvs/slipstream/bin/python -m slipstream.cli replay \
  --file python/tests/fixtures/kraken_btcusd_replay.jsonl \
  --side buy --qty 0.06 --duration 6 --slices 3
```

Run against the live Kraken book (public data, simulated fills). `scripts/demo_live.sh` starts the engine with conservative limits and runs the CLI:

```bash
PATH="$HOME/.venvs/slipstream/bin:$PATH" bash scripts/demo_live.sh --side buy --qty 0.005 --duration 60 --slices 6
```

Output of a real run (2026-09-24, Kraken BTC/USD). JSON fill logs go to stderr and the summary to stdout:

```
{"ts": "2026-09-24T02:30:28.627362+00:00", "level": "INFO", "msg": "fill", "order_id": "demo-twap-1", "qty": 0.000833, "price": 84145.8, ...}
... 5 more fills, one every ~10 s ...
order        demo-twap-1
state        COMPLETED
filled       0.005 / 0.005
avg price    84155.26
arrival mid  84145.75
slippage     1.13 bps
one-shot     0.01 bps (single market order at arrival)
saved        -1.12 bps
```

**Reading this honestly:** a 0.005 BTC order (about 420 USD) is tiny next to Kraken's top-of-book depth. A single market order only pays the spread, while the TWAP slices carry about a minute of price drift. In this run the price moved up, so slicing cost 1.12 bps more. TWAP pays off when the order is large relative to displayed depth. The engine reports both numbers on every run so that this trade-off can be measured rather than assumed. Impact-aware scheduling (Almgren-Chriss) and statistics over many runs are on the roadmap.

## Compare algorithms

`compare` submits the same parent order once per algorithm and runs them all side by side on the same feed. Each order's paper fills consume its own view of the book (until the feed refreshes a level), but orders never consume each other's liquidity, so they do not compete for it.

```bash
PATH="$HOME/.venvs/slipstream/bin:$PATH" bash scripts/demo_compare.sh --side buy --qty 0.005 --duration 1200 --slices 20
```

Output of a real run (2026-09-24 14:10–14:30 UTC, Kraken BTC/USD, buy 0.005 BTC over 20 minutes):

```
algo            state          filled      avg px  slip bps  1-shot bps  saved bps  fills
twap            COMPLETED       0.005    84343.27      8.89        0.01      -8.88     20
vwap            COMPLETED       0.005    84338.38      8.31        0.01      -8.30     20
pov             COMPLETED       0.005    84253.45     -1.77        0.01       1.77      5
almgren_chriss  COMPLETED       0.005    84264.39     -0.47        0.01       0.48     20
```

Calibrated live: σ = 5.68 $/√s and η = 166 $·s/BTC², so medium urgency gives κT ≈ 2.9 and the Almgren-Chriss schedule is front-loaded.

**Reading this honestly:**
- **Price drift decided the ranking.** BTC rose during the window, so the schedules that traded early paid less. POV finished in 5 fills because market volume was high, and Almgren-Chriss was front-loaded by design. In a falling market the same logic would rank them the other way.
- **VWAP barely moved off TWAP.** Its time-of-day profile was nearly flat across these 20 minutes.
- **One run is one sample.** Measuring each algorithm's cost distribution over many recorded sessions is the next milestone. Until then, treat this table as a demonstration of the tooling, not as evidence that one schedule beats another.

## Smart routing across venues

Taker fees on crypto venues are tens of basis points, often more than the slippage itself, so the router ranks liquidity by **all-in price**. For a buy that is `ask × (1 + fee)`; for a sell, `bid × (1 − fee)`. It walks both venues' books best-first until the child is filled.

Every fill is also priced, at the same moment and on the same books, as if the whole child had gone to each venue alone. That gives three numbers per order:
- **all-in**: slippage plus fees, as routed.
- **kraken / coinbase**: the all-in cost on that venue alone. It shows `n/a` when a venue could not have filled a child by itself.
- **gain**: the best single venue's cost minus the routed cost.

Each single-venue counterfactual consumes its own venue's liquidity exactly as the routed order consumes the venues it uses, so the comparison is like for like, simultaneous, and costs nothing.

**How fees are set.**
- Fees are configured once, on the engine: `--venue kraken:fee_bps=40 --venue coinbase:fee_bps=60`.
- The CLI reads them back from the engine and refuses to run if `--venues` doesn't match.
- The demo scripts use illustrative entry-tier taker fees. Set your own tier with `KRAKEN_FEE_BPS=… COINBASE_FEE_BPS=…`.

```bash
PATH="$HOME/.venvs/slipstream/bin:$PATH" bash scripts/demo_compare.sh --side buy --qty 0.02 --duration 600 --slices 10
```

Two live runs on the same feeds at the same time (2026-09-26 01:21–01:30 UTC, BTC/USD, buy 0.02 BTC over 10 minutes). The first uses the demo fees:

```
kraken 40 bps, coinbase 60 bps
algo            state          filled      avg px  slip bps  1-shot bps  saved bps  fee bps  all-in bps    kraken bps  coinbase bps  gain bps  fills
twap            COMPLETED        0.02    83941.44     -1.03       -0.39       0.64    40.00       38.96         38.96         59.33      0.00     10
vwap            COMPLETED        0.02    83941.60     -1.01       -0.39       0.62    40.00       38.98         38.98         59.34      0.00     10
pov             COMPLETED        0.02    83946.80     -0.39       -0.39       0.00    40.00       39.60         39.60         60.37      0.00     13
almgren_chriss  COMPLETED        0.02    83944.75     -0.64       -0.39       0.24    40.00       39.36         39.36         59.83      0.00     10
```

The second uses equal fees, to isolate the effect of price:

```
kraken 40 bps, coinbase 40 bps
algo            state          filled      avg px  slip bps  1-shot bps  saved bps  fee bps  all-in bps    kraken bps  coinbase bps  gain bps  fills
twap            COMPLETED        0.02    83940.52     -1.14       -0.39       0.75    40.00       38.85         38.96         39.23      0.11     10
vwap            COMPLETED        0.02    83940.66     -1.12       -0.39       0.73    40.00       38.87         38.98         39.24      0.11     10
pov             COMPLETED        0.02    83946.80     -0.39       -0.39       0.00    40.00       39.60         39.60         40.33      0.00      9
almgren_chriss  COMPLETED        0.02    83943.54     -0.78       -0.39       0.39    40.00       39.21         39.36         39.69      0.14     10
```

**Reading this honestly:**
- **Fees dominate.** With a 20 bps fee gap, every one of 43 fills went to Kraken. The router's answer was simply "use the cheaper venue", and the gain was zero. That is the correct answer, and it is the real lesson for small orders: your fee tier matters more than routing.
- **Routing pays when fees are close.** With equal fees, 9 of 39 fills went to Coinbase whenever its price was better. That saved 0.11 to 0.14 bps against the best single venue. It is small because a 0.02 BTC slice rarely goes past the top level of either book; the gain grows with order size relative to displayed depth.
- **Negative slippage is real, not a bug.** Kraken's ask sat about $1.20 below Coinbase's bid for the whole session. The reference mid spans both venues, so buying on the cheaper venue lands below it. The engine accepts a cross between venues like this, because fees make it unprofitable to trade. It rejects one that would still be an arbitrage after fees, and any venue whose own book is crossed.
- **One run is one sample.** As with the algorithm comparison, these tables demonstrate the tooling. They are not evidence of a statistical edge.

## Executable, honest results

Paper results are only useful if a real exchange would have accepted every fill:

- **Exchange trading rules.** At startup the demo scripts fetch each exchange's rules from its free public API: Kraken `AssetPairs` and Coinbase's public product endpoint. They pass them to the engine with `slipstream venue-flags`:
  - the minimum order size;
  - the quantity step;
  - the minimum order value.

  For example, Kraken BTC/USD requires at least 0.00005 BTC and $0.50. The engine never sends a fill below an exchange's minimum. Each slice waits until it reaches the minimum of the exchange with the best price after fees. It only goes elsewhere for a remainder that can never reach that minimum, and it never leaves a leftover no exchange would accept.
- **Risk on what is actually paid.** Each fill is routed first. The order's spending limit is then checked on the real cost, meaning the price walked through the book plus fees, before anything is committed.
- **Price collar.** A fill may never use a price more than 0.5% (50 bps) from the market mid. `--max-deviation-bps` changes it.
- **Every algorithm ends.** TWAP, VWAP, POV and Almgren-Chriss all stop at their deadline, with a reason. The table shows `filled %`, and `saved` shows `n/a` when an order or the one-shot benchmark was only partly filled, because those numbers would not be like for like. If a run fails part-way, the CLI still prints what was filled.
- **Fast by default.** The demos run an optimised release build of the engine; CI keeps the sanitizer build. Market data now flows over one stream per venue instead of per-message calls; see [Performance](#performance) for measured latency.

## Performance

`bash scripts/bench_pipeline.sh` builds the release engine and replays a deterministic synthetic stream of 50,000 events (book deltas and trades on two venues) against it in `--clock replay` mode, driving a POV order that keeps filling. It reports client-side send-to-fill latency and reads the engine's own ingest-to-processed latency from `GetStatus`. CI runs it after the Python tests and fails if p50 regresses more than 2x (3x on GitHub's shared runners) against the committed `bench/baseline.json`.

| | before (Phase 1, per unary message) | after (concurrent pipeline, per event) |
|---|---|---|
| debug + ASan | 1.28 ms | — |
| release | 0.86 ms → ~0.55 ms (removed the per-message status call) | see below |
| client p50 / p99 / p99.9 | n/a (unary, not sampled this way) | 340 µs / 0.93 ms / 3.0 ms |
| engine-side p50 / p99 (`stats`) | n/a | 33 µs / 131 µs |
| events/s in the benchmark | n/a | ~6,400 (one event in flight at a time, so this measures latency, not capacity) |

The "before" numbers are the ones already recorded in `note.md` from Phase 1 (measured over loopback with the old per-message unary calls). The "after" numbers are `bench/baseline.json`, from a local run on the development machine; a CI run of the same benchmark prints its own numbers on that run's hardware, which is why the CI gate compares against the baseline with headroom instead of asserting an absolute figure. Client-side latency is end-to-end (Python `send_nowait` to the matching `Fill` on `Subscribe`, including asyncio and gRPC overhead on both sides); the engine-side figure is the C++ engine's own ingest-to-processed time, with no Python in the loop. p99/p99.9 carry real scheduler and GC jitter from the Python side and vary more between runs than p50.

Honest reading: the engine itself is well under the 150 µs design target (33 µs p50), but the end-to-end client figure (340 µs p50) is not; most of it is Python asyncio and gRPC client overhead on WSL. The next step for end-to-end latency is on the client side (a faster event loop, batching events, or reading exchange feeds in C++).

## Hourly evidence

An hourly collector (`scripts/collect_hourly.sh`, driven by `slipstream collect run`) builds proof of what execution strategy and venue choice cost, over time rather than from one demo run:

- Every hour it runs two paper orders (0.01 and 0.25 BTC) through all four algorithms, each order size on a fresh release engine of its own: the collector starts the engine on a loopback port, runs that size, and always stops it afterwards, even when the run fails or the collector is killed. A failed size therefore can never leave orders working into the next size's run. Side alternates with the hour: buy on even UTC hours, sell on odd.
- Every result — fills, slippage, fees, routing gain, and the engine's own latency stats — is written to an append-only SQLite database at `$SLIPSTREAM_DATA_DIR/slipstream.db` (default `~/slipstream-data`, on the WSL Linux filesystem so SQLite's WAL mode works reliably). `UPDATE` and `DELETE` are blocked by triggers on every table except one allowed transition when a run finishes.
- The raw market data behind each run is kept too, gzip-compressed under `$SLIPSTREAM_DATA_DIR/recordings/`, with its SHA-256 recorded alongside. A recording under 7 days old is kept in full; from day 7 to day 90 only the 12:00 UTC hour's recording survives each day; past day 90 it is deleted. A hard cap (`SLIPSTREAM_RECORDINGS_MAX_MB`, default 1024) evicts the oldest recordings first, down to 90% of the cap, if the time-based rules alone aren't enough. The database is backed up once a day (gzip-compressed, 7 kept), and logs older than 14 days are pruned. Every hour's log includes a `disk_usage` line with the current size of recordings, database, backups, and logs.
- A run that fails (a dropped feed, a rejected order) is recorded with `status = failed` and its error; the next size still runs. An engine that fails to start is recorded the same way. A crash leaves a run `started`, which the next hour's run marks `abandoned`. Nothing is hidden.
- Register the scheduled task with `powershell -File scripts/install_task.ps1 -Install`. It is registered **disabled**: installing never starts collection. Start the hourly schedule with `-Enable` and stop it with `-Disable`; remove the task with `-Uninstall`. Once enabled it runs hourly at :05, as your own Windows user, only while you are logged on, with no stored password and standard privileges, through `conhost.exe --headless` so no console window opens. The script never runs the collector itself.

## Research page

`python/slipstream/site/` builds a static research page from `slipstream.db`: the same 3-line explainer as this README, a status column, KPIs, a 30-day all-in-cost chart per order size with a bootstrap 95% band, a fees-versus-slippage split, the last 8 runs, a `history.html` with all runs (filterable client-side, no library), a `methodology.html` with the known limits, and a `data/runs.csv` export. Every chart is server-rendered SVG from plain numbers; fonts are system stacks only; the only external link on any page is to this GitHub repository.

- Build it: `python -m slipstream.site build` (writes `$SLIPSTREAM_DATA_DIR/site/`).
- Publish it: `python -m slipstream.site publish` syncs `site/` into a clone of the public `slipstream-live` repo and pushes over SSH with a deploy key scoped to that one repo. It is a no-op until `$SLIPSTREAM_DATA_DIR/publish.enabled` exists, so nothing is ever pushed automatically before you switch it on (see `input.md`). A push happens only when something changed; a publish failure is logged and never fails the hourly collector.
- `scripts/collect_hourly.sh` runs both after every collection, failure-tolerant: the hour's evidence is already safely in the database either way.

## Feed resilience

A dropped exchange WebSocket reconnects with jittered exponential backoff (0.5 s doubling to 8 s, at most 5 reconnects per venue per run; the 6th failure ends the run). A Kraken book checksum mismatch is recovered the same way, because Kraken's documented fix is a fresh snapshot. While a venue is down its engine book is emptied at once, so nothing trades on a pre-disconnect book, and it is rebuilt only from the new connection's snapshot. If no venue has a fresh book for 30 s, the run stops. Every reconnect is logged and stored per run in the `feed_reconnects` table; recordings carry reconnect markers that replay honours.

## Security

- **Paper trading is enforced.** `SLIPSTREAM_PAPER_MODE` must be `true`, and v0.1 contains no live order path.
- **Loopback only.** The gRPC engine binds only to loopback addresses, and both sides validate the address.
- **Untrusted input.** All exchange data is validated before use: types, finiteness, ranges, and size caps. Hostile JSON (deep nesting, oversized numbers, invalid UTF-8) is rejected cleanly and never crashes the parser.
- **Venues are an allowlist** (`kraken`, `coinbase`), checked in the CLI, in the parsers, and at the engine's gRPC boundary. Market data connections go only to fixed public WebSocket URLs over TLS, with size caps. A venue whose book is stale or corrupt is excluded, or the step is skipped; the engine never guesses.
- **Risk limits hold on real costs.** The per-order spending limit is checked on the routed cost including fees, before a fill is committed. A 50 bps price collar blocks fills far from the market. Exchange rules come only from fixed public HTTPS endpoints, with no redirects, size caps, and strict number parsing.
- **Hardened C++.** It builds with `-Wall -Wextra -Wpedantic -Wshadow -Werror` and is tested under AddressSanitizer and UndefinedBehaviorSanitizer.
- **Supply chain.** Python dependencies are pinned with sha256 hashes and audited with `pip-audit`.

## Development checks

Beyond `scripts/ci.sh` (build, tests, lint, benchmark) on every push and PR, four more checks run in CI:

- **CodeQL** (`.github/workflows/codeql.yml`): security-extended queries over `python`, `c-cpp` and `actions` on push to `main`, every PR, and a weekly Monday cron. Results go to GitHub code scanning; there is no local equivalent.
- **clang-tidy** (`.github/workflows/static-analysis.yml`, config in `.clang-tidy` and `engine/tests/.clang-tidy`): runs on every engine source and test file on push to `main` and every PR. Run it locally in WSL2 Ubuntu 24.04 with `sudo apt-get install -y clang-tidy`, then `bash scripts/clang_tidy.sh`.
- **Nightly flake hunt** (`.github/workflows/nightly.yml`, `scripts/nightly.sh`): runs daily at 06:17 UTC (and on demand via `workflow_dispatch`). It builds the ASan+UBSan engine, runs the Hypothesis property tests with `SLIPSTREAM_HYPOTHESIS_PROFILE=nightly` (random seed, 2000 examples, vs. CI's derandomized 200-example `ci` profile in `python/tests/conftest.py`), then repeats the timing-sensitive tests (CLI, collector, engine stream, feed process, session, recorder, and the replay/routing/schedules integration tests) 20 times (`SLIPSTREAM_NIGHTLY_REPEATS`) to give real concurrency and wall-clock races a chance to surface. A failure uploads `nightly-output.log` as a workflow artifact. Runs locally the same way: `bash scripts/nightly.sh`.

- **Coverage** (`coverage` job in `.github/workflows/ci.yml`, `scripts/coverage.sh`): Python (pytest-cov) and C++ (gcov + gcovr) line and branch coverage on every push and PR, shown in the job summary with the HTML report as an artifact. Report only, no threshold gate. Run locally with `bash scripts/coverage.sh`; reports land in `build/coverage-report/`.

**Dependabot** (`.github/dependabot.yml`) opens weekly PRs (capped at 5 open at a time) that group minor/patch updates for GitHub Actions and for the `python/` pip dependencies into single PRs.

## Roadmap

- Batch statistics across recorded sessions (`record` + `compare --file`)
- C++ fuzz tests
- Backtest scenarios (e.g. flash-crash windows)
- Release Docker image

## License

MIT. See [LICENSE](LICENSE).

## Built with Claude Code

Developed by [@yandouziyassine](https://github.com/yandouziyassine) with [Claude Code](https://claude.com/claude-code) as an AI pair-programming agent. Commits co-authored by Claude carry a `Co-Authored-By` trailer.
