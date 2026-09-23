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

## Dev log
### 2026-09-23 — Day 1
- Brainstormed and approved the design spec, then wrote the week-1 implementation plan.
- Found no host C++ toolchain, so all builds run in an Ubuntu 24.04 dev container that CI also uses.
