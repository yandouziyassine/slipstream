# Execution Schedules (VWAP, POV, Almgren-Chriss) — Design

Date: 2026-09-24
Status: Approved
Scope: first of two week-2 sub-projects. The second sub-project, second venue plus smart routing, gets its own spec.

## 1. Purpose

v0.1 executes only TWAP. The first live run showed its limitation honestly: a small order paid 1.13 bps sliced against 0.01 bps as one market order. That happened because slicing adds exposure to price drift, and TWAP ignores both market volume and the trade-off between price risk and market impact. This project adds the three schedules real desks use:
- **VWAP:** follows the historical intraday volume profile.
- **POV:** follows live market volume.
- **Almgren-Chriss (AC):** trades off market impact against price risk.

It also adds tooling to compare all four schedules fairly on the same market data.

## 2. Architecture decision

Schedules live in the C++ engine and calibration lives in Python (approach 1 of 3):
- **The engine stays deterministic and offline.** It never calls an exchange. Python fetches all data, calibrates, and sends the parameters with the order.
- **The same safety applies everywhere.** Every schedule goes through the same risk gate and fill simulator.
- **Two alternatives were rejected.** Schedules only in Python would hollow out the engine and weaken replay tests. An engine that fetches its own data would break the no-network rule for the engine.

## 3. Engine (C++)

### 3.1 Schedule interface
`Schedule::target_qty_at(now_ns, const MarketState&) -> double`: the cumulative quantity that should be complete by `now_ns`. `MarketState` carries the engine's cumulative traded market volume. `Engine::step` is unchanged apart from calling through the interface. The risk checks (`check_child`) and `simulate_market_fill` stay as they are.

All schedules share the base parameters: total qty X, `start_ns`, `duration_ns` (T) and `num_slices` (N), with the TWAP validation rules. Slice interval τ = T / N.

| Schedule | `target_qty_at(t)` | Extra parameters and validation |
|---|---|---|
| TWAP | `X · released/N` (existing) | none |
| VWAP | `X · (w_1 + … + w_k)` for the k slices released by t | `weights`: exactly N finite values > 0; the engine normalizes them to sum to 1; the last slice returns exactly X |
| AC | `X − x(k·τ)`, where k = slices released by t (the same release rule as TWAP; k = N gives exactly X) | `sigma`, `eta`, `risk_aversion`: finite, > 0 |
| POV | `min(X, p · (V(t) − V_submit))` | `participation` p with 0 < p ≤ 0.5; the order halts with reason `deadline reached` once t ≥ start + T while incomplete |

### 3.2 Almgren-Chriss math
- **Model.** Linear temporary impact h(v) = η·v, permanent impact γ = 0 (a documented v1 simplification), and risk aversion λ.
- **Exact discrete-time κ:** `κ = acosh(1 + λσ²τ² / (2η)) / τ`.
- **Remaining holdings:** `x(t) = X · sinh(κ(T − t)) / sinh(κT)`.
- **Numerical stability.** The ratio is evaluated as `(e^{−κt} − e^{−κ(2T−t)}) / (1 − e^{−2κT})`, so it cannot overflow for large κT. When κT < 1e-9 it uses the linear (TWAP) limit.
- **Urgency presets for λ (per USD):** `low = 3e-6`, `medium = 3e-5`, `high = 3e-4`. With typical BTC conditions (σ ≈ 5.7 $/√s, η ≈ 100 $·s/BTC) and a 10-minute order, these give κT ≈ 0.6, 2 and 6. `--risk-aversion` overrides the preset.

### 3.3 Market volume
- **New RPC:** `ApplyTrades(TradeBatch{symbol, repeated Trade{price, qty, ts_ns}}) -> TradeAck`.
- **Validation:** same symbol check as book updates; each trade must have finite, positive price and qty; at most 1000 trades per batch.
- **Volume tracking.** The engine keeps a cumulative traded volume V. Each POV order stores V at submit time.

### 3.4 Proto
`ParentOrder` gains:
```
oneof schedule { TwapParams twap = 7; VwapParams vwap = 8; AlmgrenChrissParams almgren_chriss = 9; PovParams pov = 10; }
```
If the field is unset, the order is TWAP, so v0.1 clients keep working. `OrderStatus` gains `string algo`.

## 4. Orchestrator (Python)

### 4.1 `kraken_rest.py`
- **Requests.** HTTPS GET to the fixed host `api.kraken.com`, path `/0/public/OHLC`, using stdlib `urllib`. Timeout 10 s, 2 MiB response cap, default TLS verification.
- **Parsing.** Only `KrakenMessageError` may escape, covering the same hostile-input cases as the WebSocket parser. A non-empty `error` array in the response is also an error.
- **Pair mapping.** A table maps `BTC/USD` to the REST pair `XBTUSD`; the data key in the response is the single key other than `last`.
- **Fetches.** `interval=15` returns about 7.5 days of bars for the VWAP profile. `interval=1` returns 12 hours of bars for σ.

### 4.2 `calibration.py` (pure functions)
- **`vwap_weights(bars_15m, start_ns, duration_s, slices)`.** Builds a 96-bucket average volume by UTC time of day, and gives each slice the bucket average at its midpoint. Executions shorter than one bucket get near-equal weights; this is documented, not hidden.
- **`estimate_sigma(bars_1m)`.** The standard deviation of 1-minute close-to-close changes divided by √60, in $/√s. It needs at least 60 bars.
- **`estimate_eta(book, side, slice_qty, tau)`.** Walks the arrival book for sizes of 0.5×, 1×, 2× and 4× the slice, fits a least-squares line of average fill cost above the touch against size, and sets η = slope × τ. It needs at least 3 fill points and a positive slope.
- **Failure.** Any calibration failure means the order is not submitted and the process exits 1 with a clear reason.

### 4.3 Trade channel
- **Subscription.** The live loop subscribes to `book` and `trade`.
- **Parsing.** `parse_message` returns `BookUpdate | TradeBatch | None`.
- **Historical trades.** Trade messages of type `snapshot` are ignored, because they are historical and would inflate POV's volume.
- **Forwarding.** Trade updates are forwarded through `ApplyTrades`.

### 4.4 CLI
- **`live` and `replay`** gain `--algo twap|vwap|pov|ac` (default `twap`), `--urgency low|medium|high` (default `medium`), `--risk-aversion`, and `--participation` (default 0.1).
- **`compare live|replay --algos twap,vwap,pov,ac`** submits one parent order per algorithm, all with the same side, qty and horizon, with ids `cmp-<algo>-<ts>`. It runs until every order is terminal or the deadline passes, then prints one table: algo, state, filled, avg price, slippage bps, one-shot bps, saved bps, fills. The orders share one position, so the engine limits must allow N × qty.
- **`record --duration S --out FILE`** needs no engine. It first writes the two OHLC responses as `{"kind":"ohlc","interval":…,"data":…}` lines, then every book and trade message as `{"recv_ns":…,"msg":…}` lines. It refuses to overwrite an existing file.
- **Replay** reads the recorded OHLC lines for calibration and never fetches live data. If VWAP or AC is requested and the file has no OHLC lines, it exits 1 with an explicit error.

### 4.5 Runner
- `ExecutionRunner` takes a list of order specs.
- On the first book snapshot it calibrates and submits every order.
- It forwards trade batches and steps the engine clock on every message.
- `is_done` means every order is terminal.
- One formatter produces both the single-order summary and the comparison table.
- Calibrated values (σ, η, κT, weights) are logged as structured JSON.

## 5. Testing
- **C++ (GoogleTest, ASan + UBSan):**
  - Per-schedule unit tests: VWAP normalization and rejection of bad weights, AC reducing to TWAP as κ → 0 within 1e-9, AC being front-loaded compared with TWAP, AC ending at exactly X, AC not overflowing at κT = 700, POV target, cap and deadline halt.
  - `ApplyTrades` validation.
  - Service mapping of the `oneof`.
  - A randomized invariant test across all schedules: the target never decreases in t and stays within [0, X]. Full property-based testing (rapidcheck) remains week-3 work.
- **Python:**
  - Calibration checked against hand-computed σ, η and weights from fixtures.
  - The OHLC and trade parsers get the hostile-input suite.
  - Recording then replaying gives identical results.
  - Comparison table formatting.
  - End-to-end: a recorded fixture with book, trades and OHLC runs all four algorithms through the real engine with exact assertions.
- **CI:** existing `scripts/ci.sh`, unchanged.

## 6. Security
- **New outbound traffic** is limited to HTTPS GET requests to a hard-coded Kraken host, with TLS verified, a timeout and a size cap. The URL is not configurable from the CLI.
- **The recorder** writes only to the path the user gives and never overwrites an existing file.
- **All new inputs are validated at the boundary.** On the Python side that means OHLC data, trade messages and the new CLI flags. On the C++ side it means the new proto fields and `ApplyTrades`.
- **`/security-review`** runs before each merge.

## 7. Delivery
Three PRs. PR 1 and PR 2 are independent and built in parallel worktrees.
1. **Engine:** `Schedule` interface, VWAP, AC, POV, `ApplyTrades`, and the proto `oneof` plus `algo` field.
2. **Python data:** `kraken_rest`, `calibration`, the trade channel parser, and `record`.
3. **Integration:** `--algo`, `compare`, the runner generalization, the four-algorithm end-to-end test, and the README with a real comparison run.

## 8. Out of scope
- Permanent impact γ.
- Re-calibrating during an order.
- Multiple venues and routing (next spec).
- Batch statistics across recorded sessions (week 3).
- Live trading.
