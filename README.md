# Slipstream

Open-source smart order execution engine. Slipstream slices a large order over time (TWAP), checks every slice against hard risk limits in C++ before it fills, and reports the slippage it paid against what a single market order would have cost at arrival.

> **Status: v0.1, in development. Paper trading only.** Slipstream streams Kraken's public BTC/USD order book. It uses no API keys, places no real orders, and moves no real money.

## Why

A large market order moves the price against the trader who sends it (market impact). Institutional desks pay for execution algorithms and smart order routers to reduce that cost. Retail traders and small funds usually send one market order and absorb the slippage. Slipstream brings the same approach into the open, where anyone can audit and measure it.

## How it works

```
Kraken WS v2 (public) ──► Python orchestrator ──gRPC (loopback)──► C++20 engine
                          validate + parse                         order book (depth-capped)
                          drive the clock                          TWAP schedule
                          JSON logs + summary                      risk gate ──► simulated fills
```

- **Deterministic engine.** The caller supplies the clock (`now_ns`), so live runs, replays, and tests all exercise one code path.
- **Risk before every fill.** The engine enforces a per-order notional budget (slippage included) and a maximum absolute position. A breach halts the order; it never skips the check.
- **Measured outcome.** Each order reports its average fill price and its slippage against the arrival mid. It also reports the cost one market order for the full size would have paid at arrival (the "one-shot" benchmark).

## Tech stack

| Layer | Technology |
|---|---|
| Engine | C++20, gRPC 1.51, Protocol Buffers, CMake + Ninja, GoogleTest, ASan + UBSan |
| Orchestrator | Python 3.12, grpcio, websockets, pytest, `mypy --strict`, ruff |
| Contract | `proto/slipstream/v1/execution.proto` |
| Tooling | Ubuntu 24.04 dev container, hash-pinned dependencies, pip-audit, GitHub Actions |

## Repository layout

```
engine/            C++ engine: order book, TWAP, risk, fill simulator, gRPC service
python/slipstream  Python orchestrator: Kraken parser, engine client, runner, replay, live feed, CLI
proto/             gRPC contract shared by both sides
scripts/           build, codegen, and demo scripts
docs/superpowers/  design spec and implementation plans
```

## Build and test (Ubuntu 24.04 or WSL)

```bash
sudo apt-get install -y cmake ninja-build pkg-config libgrpc++-dev libprotobuf-dev \
  protobuf-compiler protobuf-compiler-grpc libgtest-dev python3-venv

bash scripts/build_engine.sh
ctest --test-dir build/engine --output-on-failure

python3 -m venv .venv
.venv/bin/pip install --require-hashes -r python/requirements-dev.txt
PYTHON=.venv/bin/python bash scripts/gen_proto.sh
(cd python && ../.venv/bin/python -m pytest -q)
```

A Docker dev container (`compose.yaml`, `docker/dev.Dockerfile`) wraps the same toolchain.

## Run a paper execution

Replay a recorded order book:

```bash
build/engine/slipstream_engine --listen 127.0.0.1:50051 --max-order-notional 10000 &
PYTHONPATH=python .venv/bin/python -m slipstream.cli replay \
  --file python/tests/fixtures/kraken_btcusd_replay.jsonl \
  --side buy --qty 0.06 --duration 6 --slices 3
```

Run against the live Kraken book (public data, simulated fills):

```bash
PYTHONPATH=python .venv/bin/python -m slipstream.cli live --side buy --qty 0.005 --duration 60 --slices 6
```

## Security

- **Paper trading is enforced.** `SLIPSTREAM_PAPER_MODE` must be `true`, and v0.1 contains no live order path.
- **Loopback only.** The gRPC engine binds only to loopback addresses, and both sides validate the address.
- **Untrusted input.** All exchange data is validated before use: types, finiteness, ranges, and size caps. Hostile JSON (deep nesting, oversized numbers, invalid UTF-8) is rejected cleanly and never crashes the parser.
- **Hardened C++.** It builds with `-Wall -Wextra -Wpedantic -Wshadow -Werror` and is tested under AddressSanitizer and UndefinedBehaviorSanitizer.
- **Supply chain.** Python dependencies are pinned with sha256 hashes and audited with `pip-audit`.

## Roadmap

- VWAP and implementation-shortfall (Almgren-Chriss) algorithms
- Second venue and smart routing across venues
- Property-based and fuzz tests; Kraken book checksum verification
- Backtest scenarios (e.g. flash-crash windows)
- Release Docker image

## License

MIT. See [LICENSE](LICENSE).

## Built with Claude Code

Developed by [@yandouziyassine](https://github.com/yandouziyassine) with [Claude Code](https://claude.com/claude-code) as an AI pair-programming agent. Commits co-authored by Claude carry a `Co-Authored-By` trailer.
