# Slipstream — Project Notes

## What it is
Slipstream is an open-source smart order execution engine. Instead of sending one large market order, it slices the order over time (TWAP) and checks every slice against hard risk limits. It then measures slippage against what a single market order would have cost at arrival. v1 paper-trades BTC/USD against Kraken's live public order book.

## The real-world problem
Large orders move the price against the trader (market impact). Institutions pay for execution algorithms and smart order routers to reduce this cost. Retail traders and small funds usually send one market order and absorb the slippage. Slipstream brings the same tools into the open, where they can be audited and measured.

## Architecture
    Kraken WS (public)   -+
                          +-> Python orchestrator --gRPC (loopback)--> C++ engine
    Coinbase WS (public) -+   parse + validate                           one book per venue
                              drive clock                                schedules (TWAP/VWAP/POV/AC)
                              JSON logs + summary                        smart router (price + fee)
                                                                         risk checks -> simulated fills

- The engine is deterministic. The caller supplies `now_ns`, so live runs, replays, and tests share one code path.
- Risk checks live in C++ and run before every fill. Live mode, when added later, must pass the same gate.

## Key decisions
| Decision | Why |
|---|---|
| Two services over gRPC | Clean contract (`execution.proto`), independent testing, C++ hot path + Python glue, the same split used on real trading desks |
| Caller-supplied clock | Deterministic tests and replays with no hidden time dependency |
| Paper mode only in v1 | Prove correctness before any real money is at risk |
| Kraken public WebSocket v2 | Free, no API key, well documented |
| Ubuntu 24.04 dev container | No host C++ toolchain needed; identical environment locally and in CI |
| Hash-pinned Python deps | Supply-chain protection |
| Loopback-only gRPC | No network exposure without TLS and auth |
| Route by all-in price (price + taker fee) inside the C++ engine | Fees often exceed slippage on crypto venues; routing inside the engine keeps one deterministic risk gate over the total position |
| Fees configured once on the engine; Python reads them back from `GetStatus` | Two fee configs would drift; the CLI refuses to run when `--venues` differs from the engine's venues |
| Coinbase parser mirrors the full book and sends a top-N view | Coinbase sends full-book deltas but the engine keeps only N levels, so deleted top levels could never be refilled |
| Cross between venues is allowed within fees; a cross that survives fees, or a venue whose own book is crossed, skips the step | Live venues sit crossed by ~0.15 bps all day because fees make it unprofitable; only a post-fee arbitrage or a self-crossed book signals bad data |

## Known limitations (v1)
- Paper fills do not consume book liquidity. Each fill assumes the displayed book is still there at the next step.
- Prices and quantities are `double`. Fixed-point decimals are planned before any live trading.
- The Kraken book checksum is not verified yet. With two venues, a corrupted book is caught only if it crosses itself or crosses the other venue by more than the fees.
- `record` to a Windows path from WSL once captured only 2.7 s of a 15 s window: both feeds' first messages arrived about 12 s late. The cause is not our parsing (62 ms for the 5 MB Coinbase snapshot) and not DNS (20-60 ms); the suspect is a blocked filesystem write across WSL. Open item: time the connect, snapshot and write phases separately.
- The slippage comparison includes market drift during the execution window. It illustrates one run; it does not prove an edge statistically.
- If one of several orders submitted together is rejected by the engine, the orders accepted before it keep working in that engine process; the CLI exits with an error. The demo scripts start a fresh engine per run and stop it on exit. A cancel RPC is planned.

## Dev log
### 2026-09-23 — Day 1
- Brainstormed and approved the design spec, then wrote the week-1 implementation plan.
- Found no host C++ toolchain, so all builds run in an Ubuntu 24.04 dev container that CI also uses.
- Docker Desktop crashed on startup: two stale AF_UNIX socket files in `%LOCALAPPDATA%\Docker\run` (`dockerInference`, `userAnalyticsOtlpHttp.sock`) could not be removed. Docker work is parked until the end of the week. Meanwhile, the Python track runs in a host venv (Python 3.13); the code targets 3.12+.
- Scaffold (`chore/scaffold`): repo hygiene, CLAUDE.md, a hash-pinned dependency lock (917 sha256 hashes, all platforms), dev container definition, and the gRPC contract `execution.proto`.
- Python orchestrator (`feat/orchestrator-core`, then `feat/replay-live-cli`): Kraken v2 parser, settings with a paper-mode gate, typed gRPC client, execution runner, JSONL replay, live WebSocket loop, JSON logging, and CLI. 79 tests; `mypy --strict` and `ruff` clean.
- Review findings fixed (test-first): hostile Kraken messages could crash the parser with non-domain exceptions. The triggers were deeply nested JSON (`RecursionError`), integers over 4300 digits (`ValueError`), invalid UTF-8 (`UnicodeDecodeError`), and huge prices or quantities overflowing `float()` (`OverflowError`). All now surface as `KrakenMessageError`. The replay reader got the same treatment.
- mypy strict now skips generated gRPC stubs (`exclude` plus scoped overrides) but still checks all project modules.
- C++ engine (`feat/engine-core`) built in WSL Ubuntu 24.04 with the same apt packages as the dev container: order book, TWAP, risk gate, fill simulator, engine, config, and gRPC service. 52 GoogleTest cases pass under `-Werror` with ASan + UBSan.
- Engine review findings fixed: a signed int64 overflow in `TwapSchedule::target_qty_at` at extreme `now_ns` (reachable via gRPC `Step`; UBSan abort). Locale-dependent `<cctype>` checks were replaced with explicit ASCII checks for ids, symbols, and numbers.
- `/security-review` on both code branches: no findings.
- Published to GitHub (MIT). Four stacked PRs were merged into `main`. Commits use the GitHub noreply address to keep the author's email private.

### 2026-09-24 — Day 2
- End-to-end test: the recorded Kraken-format replay runs through the real C++ engine over gRPC and matches the hand-computed fills (0.83 bps TWAP vs 1.67 bps one-shot).
- CI: `scripts/ci.sh` covers codegen, sanitizer build, ctest, ruff, mypy, pytest, and pip-audit. GitHub Actions runs it on Ubuntu 24.04 with the same apt toolchain.
- Docker fixed: deleted the stale sockets from WSL (Windows could not remove the AF_UNIX reparse points). The dev image now builds.
- First live paper run on Kraken BTC/USD (0.005 BTC, 6 slices over 60 s): TWAP 1.13 bps vs one-shot 0.01 bps. A small order next to deep top-of-book liquidity only pays the spread when sent at once, while slicing adds exposure to price drift. This is expected, and it motivates impact-aware scheduling (Almgren-Chriss) and multi-run statistics in week 2.

### 2026-09-24 — Week 2: execution schedules
- Spec, plan, and three PRs (#7 market data, #8 engine, PR 3 integration). The engine and Python tracks were built in parallel worktrees.
- Engine: one `Schedule` interface with TWAP, VWAP (normalized volume weights), POV (share of live volume, halts at the deadline), and Almgren-Chriss. For Almgren-Chriss, κ uses the exact discrete-time solution (acosh), the trajectory is evaluated with only non-positive exponents so it cannot overflow, and it falls back to linear as κT → 0. A randomized test checks that every schedule's target never decreases and stays within [0, X].
- Calibration: the VWAP profile comes from Kraken 15-minute candles, σ from 1-minute candles, and η from the book's depth curve. The recorder saves the candle responses so replays reproduce calibration exactly.
- Live bug found by the first real comparison: the original η estimator probed costs at multiples of the slice size. Kraken's best level holds far more than a slice, so the measured impact was zero and calibration refused to run. The fail-safe worked, and no order was submitted. It was replaced with the depth-curve fit and a regression test built from a realistic deep book.
- Security review fix: the OHLC client refuses HTTP redirects, because urllib would otherwise follow an https-to-http downgrade.
- Four-algorithm end-to-end test against the real engine: every fill matches a hand derivation, including VWAP weights across a 15-minute bucket boundary, POV volume timing, and Almgren-Chriss at κτ = 0.5.
- First live comparison (20 min, 0.005 BTC): POV −1.77 bps, Almgren-Chriss −0.47, VWAP +8.31, TWAP +8.89. BTC drifted up during the window, which rewarded the schedules that traded early. Calibrated values: σ = 5.68 $/√s, which matches the 5.7 assumed in the spec, and η = 166. One run is not evidence of an edge; batch statistics come next.

### 2026-09-25 — Week 2: smart routing across venues (PRs #10–#13)
- Spec, plan, then three PRs. #11 (engine: per-venue books, staleness, fee-aware router, counterfactual per-venue costs) and #12 (Python: Coinbase parser, concurrent feeds, multi-venue record and replay) were built in parallel worktrees; PR 3 wires them together. Each task was followed by `/security-review` and `/caveman:caveman-review`.
- Bug found while planning PR 3: the engine keeps only the top 10 levels per venue and truncates after every delta. Kraken refills that window itself, but Coinbase sends full-book deltas, so every deleted top level made the Coinbase book thinner. Fix: the parser mirrors the full book (bisect-sorted, capped at 200 000 levels per side) and sends a top-N snapshot on every message. It also fails safe on an update that arrives before any snapshot.
- Bug found by the first live two-venue run: every order was rejected with "no market data". A recording showed Coinbase's bid sitting $1.24 above Kraken's ask in 43 of 43 book states, and the engine refused any crossed consolidated book. Root cause: the spec treated a cross between venues as stale data. Fix, test-first: a venue whose own book is crossed, or a cross that survives fees, still skips the step; a cross within fees is normal data. The failing recording now replays to a completed order.
- End-to-end test through the real engine with hand-derived legs, including a case where the fee flips the choice (Kraken's 100030 beats Coinbase's cheaper-gross 100025 at 1 bps). Every number matched on the first run.
- Live result (0.02 BTC over 10 minutes, two runs at the same time): with 40/60 bps fees, all 43 fills went to Kraken and the gain was 0. With equal 40/40 fees, 9 of 39 fills went to Coinbase, saving 0.11-0.14 bps against the best single venue. The fee tier matters more than routing for small orders. Calibrated η on the consolidated book was 19.4, against 166 on Kraken alone in the previous run. A deeper combined book should lower the estimated impact, but those runs were on different days, so this is one sample, not a measurement.
