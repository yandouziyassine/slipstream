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

Run against the live Kraken book (public data, simulated fills). `scripts/demo_live.sh` starts the engine with conservative limits and runs the CLI:

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/demo_live.sh --side buy --qty 0.005 --duration 60 --slices 6
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

`compare` submits the same parent order once per algorithm and runs them all side by side on the same feed. Paper fills do not consume liquidity, so the orders do not compete for it.

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/demo_compare.sh --side buy --qty 0.005 --duration 1200 --slices 20
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

## Security

- **Paper trading is enforced.** `SLIPSTREAM_PAPER_MODE` must be `true`, and v0.1 contains no live order path.
- **Loopback only.** The gRPC engine binds only to loopback addresses, and both sides validate the address.
- **Untrusted input.** All exchange data is validated before use: types, finiteness, ranges, and size caps. Hostile JSON (deep nesting, oversized numbers, invalid UTF-8) is rejected cleanly and never crashes the parser.
- **Hardened C++.** It builds with `-Wall -Wextra -Wpedantic -Wshadow -Werror` and is tested under AddressSanitizer and UndefinedBehaviorSanitizer.
- **Supply chain.** Python dependencies are pinned with sha256 hashes and audited with `pip-audit`.

## Roadmap

- Batch statistics across recorded sessions (`record` + `compare --file`)
- Second venue and smart routing across venues
- Property-based and fuzz tests; Kraken book checksum verification
- Backtest scenarios (e.g. flash-crash windows)
- Release Docker image

## License

MIT. See [LICENSE](LICENSE).

## Built with Claude Code

Developed by [@yandouziyassine](https://github.com/yandouziyassine) with [Claude Code](https://claude.com/claude-code) as an AI pair-programming agent. Commits co-authored by Claude carry a `Co-Authored-By` trailer.
