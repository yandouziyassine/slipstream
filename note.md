# Slipstream — Project Notes

## What it is
Slipstream is an open-source smart order execution engine. Instead of sending one large market order, it slices the order over time (TWAP) and checks every slice against hard risk limits. It then measures slippage against what a single market order would have cost at arrival. v1 paper-trades BTC/USD against Kraken's live public order book.

## The real-world problem
Large orders move the price against the trader (market impact). Institutions pay for execution algorithms and smart order routers to reduce this cost. Retail traders and small funds usually send one market order and absorb the slippage. Slipstream brings the same tools into the open, where they can be audited and measured.

## Architecture
    Kraken WS (public) -> Python orchestrator --gRPC (loopback)--> C++ engine
                          parse + validate                           order book
                          drive clock                                TWAP schedule
                          JSON logs + summary                        risk checks -> simulated fills

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

## Known limitations (v1)
- Paper fills do not consume book liquidity. Each fill assumes the displayed book is still there at the next step.
- Prices and quantities are `double`. Fixed-point decimals are planned before any live trading.
- The Kraken book checksum is not verified yet (planned for week 2).
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
