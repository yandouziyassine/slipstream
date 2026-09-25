# Second Venue + Smart Order Routing — Design

Date: 2026-09-24
Status: Approved
Scope: second of the two week-2 sub-projects. It follows the execution-schedules spec (`2026-09-24-execution-schedules-design.md`).

## 1. Purpose

Slipstream so far executes on one venue, Kraken. A smart order router (SOR) looks at several venues at once and takes the cheapest liquidity wherever it sits. On crypto venues, "cheapest" must include **taker fees**, which are tens of basis points and often larger than slippage itself. This project adds Coinbase as a second venue and routes every child slice across both venues by all-in cost (price plus fee). Paper trading only.

It also measures the router's value on every run. For each child, the engine prices the same quantity routed and on each single venue, all at the same moment and on the same books.

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Second venue | Coinbase Advanced Trade public WebSocket (`wss://advanced-trade-ws.coinbase.com`) | Largest US spot venue. `level2` and `market_trades` are public and "a JWT is not required" (Coinbase docs) |
| Routing | Split each child across venues by fee-adjusted price, walking a consolidated book best-first | What real SORs do; a single-venue choice leaves money on the table when one top level is thin |
| Fees | Per-venue taker fee in bps, supplied as explicit engine config | Real fee tiers depend on each user's volume; fetching them needs authenticated keys, which Slipstream never uses |
| Proof of value | Counterfactual pricing of every child on each single venue | Paper fills don't consume liquidity, so the comparison is free, simultaneous, and fair |
| Architecture | C++ engine owns the venue books, routing, and risk; Python adds a Coinbase adapter and runs both feeds at once | Keeps one deterministic safety gate. Rejected: routing in Python (outside the risk gate and replay), and one engine per venue (split positions, no consolidated limit) |

## 3. Engine (C++)

### 3.1 Venues
- Each venue is registered at startup with a repeatable `--venue NAME:fee_bps=F` flag.
- `NAME` must come from the allowlist `{kraken, coinbase}`, and `F` must be finite and in [0, 1000]. The engine must have at least one venue.
- Default when no `--venue` is given: `kraken:fee_bps=0`. That keeps v0.2 behavior (single venue, gross prices) and all existing tests unchanged.
- `BookUpdate` and `TradeBatch` gain a `string venue` field, and `BookUpdate` also gains `recv_ns` (§3.2).
  - An unknown or unregistered venue is rejected with `INVALID_ARGUMENT`.
  - An empty venue is accepted only when exactly one venue is registered, and then it means that venue. This keeps single-venue clients compatible.

### 3.2 Books and staleness
- The engine keeps one `OrderBook` per venue, plus that venue's `last_update_ns`.
- `BookUpdate` gains `int64 recv_ns`: the caller-supplied receive time, the same clock as `Step.now_ns`. The engine never reads a wall clock. Values below 0 are rejected. `last_update_ns` is the latest `recv_ns` applied for that venue.
- A venue is **stale** when `now_ns − last_update_ns > stale_ns`, set by `--stale-ms` (default 2000). Stale venues are left out of routing and out of the reference price. If no fresh venue with liquidity on the needed side remains, the child waits for the next step. Deadlines, such as POV's, still apply.
- The staleness check runs only when **two or more** venues are registered. Single-venue engines behave exactly as before, so v0.2 clients that send no `recv_ns` keep working.

### 3.3 Router
- **Effective price.**
  - Buy: `ask × (1 + fee)`.
  - Sell: `bid × (1 − fee)`.
  - Here `fee = fee_bps / 10⁴`.
- **Walk.** The router merges the eligible venues' levels by effective price, best first, and walks them until the child quantity is filled or liquidity runs out.
  - Ties are broken by venue registration order, which makes the result deterministic.
  - The result is a list of legs, one per venue used: `{venue, qty, gross_avg_price, fee_paid}`.
  - A partial fill leaves the remainder for the next step, as before.
- **Reference price (consolidated mid).** Take the best bid and best ask across the fresh venues, using gross prices, and use their midpoint.
  - If the consolidated book is crossed (best bid ≥ best ask), the engine skips the step and tries again on the next one. It never trades into a cross that is probably stale.
  - With a single venue this is exactly the old `mid()`.
- **Risk.** Checks are unchanged in structure. `check_parent` and `check_child` use the consolidated mid. The notional budget and position limits apply to the **total** across venues. Fees are not added to the notional budget; they are reported separately.

### 3.4 Counterfactuals and reporting
- On each child fill the engine also computes, for every registered fresh venue `v`, the all-in cost of filling the same quantity on `v` alone. It uses the same walk, restricted to one venue.
- If `v` cannot fill the whole quantity, its counterfactual counts as unavailable for that child and is reported as such, never as a number built from a partial fill.
- `OrderStatus` gains:
  - `fees_paid` (quote currency) and `fees_bps`, both relative to the filled notional.
  - `routed_all_in_bps`: gross slippage plus fees against the arrival mid.
  - `repeated VenueCost venue_costs`, each `{venue, all_in_bps, available}`, aggregated over the order's children. A venue whose counterfactual was unavailable for any child has `available = false`.
- **"Saved vs best single venue"** is computed in Python: `min(available venue all_in_bps) − routed_all_in_bps`.
- `Fill` gains `venue` and `fee`. One routed child can produce up to two fills, one per venue leg.

## 4. Orchestrator (Python)

### 4.1 `coinbase.py`
- **Contract.** Strict parser for Coinbase Advanced Trade WebSocket messages, returning `BookUpdate | TradeBatch | None` with `venue="coinbase"`. Hostile input may only ever raise `CoinbaseMessageError(ValueError)`.
- **Subscriptions.** `level2`, `market_trades`, and `heartbeats`. The heartbeats subscription keeps the connection open.
- **Symbols.** `BTC/USD` maps to `BTC-USD`.
- **Book.** `level2` snapshot and update events are parsed from string `price_level` / `new_quantity`. A quantity of `0` deletes the level.
  - The full-book snapshot is size-capped at 16 MiB and truncated to the engine's book depth, best levels first, before it is sent to the engine.
  - *Amended 2026-09-25 (PR 3).* The parser keeps the full book and emits a top-N snapshot after every level2 message. It no longer passes updates through as deltas.
    - Why: Coinbase sends deltas for the whole book, but the engine keeps only the top N levels. When a top level was deleted, the engine could never refill it, so the book thinned out over a session.
    - The level count is capped at 200 000 per side, and an update that arrives before any snapshot raises an error.
- **Trades.** `market_trades` update events become a `TradeBatch`. Snapshots are ignored, as on Kraken.
- **Sequence numbers.** A gap in the per-connection `sequence_num` raises `CoinbaseMessageError("sequence gap")`. That stops the session safely instead of trading on a desynced book.
- **Exact field names.** They are pinned in the implementation plan from Coinbase's AsyncAPI reference, and every field is validated: types, finiteness, ranges, list sizes.

### 4.2 Venue tagging and multiple feeds
- `BookUpdate` and `TradeBatch` gain `venue: Venue` (`Literal["kraken", "coinbase"]`), and the Kraken parser sets `venue="kraken"`.
- `live.run_live` connects one WebSocket per configured venue inside an `asyncio.TaskGroup`. Every message goes to one shared runner, stamped with `time.time_ns()`. If any feed errors or goes idle, the session ends with `LiveFeedError`.
- The runner checks the symbol per venue. It submits only after **every** configured venue has delivered its first book snapshot. It forwards non-snapshot trades with their venue.
- Calibration sources are unchanged:
  - σ and the VWAP profile come from Kraken candles.
  - η is fitted on the consolidated fee-adjusted book, reusing the depth-curve estimator with each venue's levels merged by effective price.

### 4.3 Record and replay
- Recorded lines gain `"venue"`. A replay feeds the interleaved multi-venue stream in `recv_ns` order.
- Lines without `"venue"` are treated as Kraken, so existing fixtures and recordings keep working.

### 4.4 CLI
- `--venues kraken,coinbase` (default `kraken`) is added to `live`, `replay`, `compare`, and `record`, validated against the allowlist.
- *Amended 2026-09-25 (PR 3).* Fees are configured once, on the engine.
  - `GetStatus` reports each registered venue with its fee (`StatusReply.venues`).
  - The CLI reads that list, refuses to run when it differs from `--venues`, and passes the fees to the η calibration. No second fee config can drift.
- Fees are engine flags. `scripts/demo_*.sh` pass `--venue kraken:fee_bps=… --venue coinbase:fee_bps=…` with clearly labeled illustrative values, and the README tells users to set their own tier.
- `format_summary` and `format_comparison` gain columns for fees (bps), routed all-in (bps), each venue's all-in (bps, or `n/a`), and saved vs best single venue (bps).

## 5. Testing
- **C++:**
  - Router: effective-price ordering with fees; a thin top level on one venue spills onto the other; buy and sell sides; tie-break by registration order; zero fees on one venue reproduce the single-book fill exactly.
  - Staleness exclusion, crossed-book skip, the unknown-venue rejection, and the empty-venue fallback.
  - Counterfactual values against hand calculations, including "unavailable".
  - A randomized invariant: routed all-in cost ≤ min(available single-venue all-in cost) + 1e-9.
- **Python:**
  - The Coinbase parser against the hostile-input suite (deep nesting, huge numbers, invalid UTF-8, sequence gaps, mixed products, oversized messages).
  - Snapshot truncation.
  - Two feeds in one `TaskGroup` against local `ws://` servers, including one feed dying.
  - Record and replay of a multi-venue session, with backward compatibility for venue-less files.
- **End to end:** a replayed two-venue fixture through the real engine, with hand-derived legs showing the spill and the effect of the fee difference.
- **Live:** a two-venue `compare` run, with the real table in the README and an honest reading.

## 6. Security
- **Network.** The new outbound connection goes to a fixed Coinbase WebSocket URL only, over TLS, with size caps. There are no keys and no authenticated channels.
- **Allowlists.** Venue names are checked against the allowlist on both sides, at the Python parsing and CLI boundary and at the C++ config and gRPC boundary.
- **Config.** Fees and staleness are validated at engine startup.
- **Process.** Every implementation task is followed by `/security-review` and `/caveman:caveman-review` before the next task starts. Findings are fixed test-first.

## 7. Delivery
Three PRs. PR 1 and PR 2 are independent and built in parallel worktrees.
1. **Engine:** venue registry and config, per-venue books and staleness, router, counterfactuals, proto changes.
2. **Python:** Coinbase parser, venue tagging, multi-feed `live`, multi-venue record/replay.
3. **Integration:** `--venues`, report columns, runner multi-venue submission, two-venue end-to-end test, live comparison, docs.

## 8. Out of scope
- Real order placement on any venue.
- Maker/limit orders, including rebates and queue position.
- Latency and fill-probability modeling.
- More than two venues. The design generalizes, but only two ship.
- Cross-venue position transfer and settlement.
