# Phase 1: Accuracy, Safety and Latency Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bugs found by the 2026-09-26 code review and system design review so that paper results are executable, the risk limits hold on the prices actually paid, orders always end, and the hot path does less blocking work. There are no architecture changes; those belong to Phase 2.

**Decisions made by the user (2026-09-26):**
- **Venue trading rules are fetched at startup.** They come from the free public exchange APIs (Kraken `AssetPairs`, Coinbase Advanced Trade public product).
- **The price collar is 50 bps from the reference mid.**
- **Scope is Phase 1 only.** Phase 2 (a single async RPC per event, reconnect, an engine-owned clock, fill sequence numbers and cancel, liquidity consumption, Kraken checksum) comes after a design pass.

**Verified exchange rules (fetched 2026-09-26):**

| Venue | Endpoint | Min qty | Qty step | Min notional |
|---|---|---|---|---|
| Kraken | `GET https://api.kraken.com/0/public/AssetPairs?pair=XBTUSD` → `result.XXBTZUSD` | `ordermin` = "0.00005" | `lot_decimals` = 8, so step = 1e-8 | `costmin` = "0.5" |
| Coinbase | `GET https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD` | `base_min_size` = "0.00000001" | `base_increment` = "0.00000001" | `quote_min_size` = "1" |

Coinbase also returns `status` ("online") and `trading_disabled` (false).

**Rules for every task (CLAUDE.md):**
- **TDD:** red, then green, then refactor.
- **Reviews:** after each task, run `/security-review` and `/caveman:caveman-review`, and fix valid findings test-first.
- **Commits:**
  - Set the author to the noreply address through `GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL`. Never write it with `git config`.
  - Add the `Co-Authored-By` trailer to every commit.
  - Run git from Windows. WSL git can't resolve the worktree.
- **Build and test:** in WSL, through a script file:
  - `MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/c/.../script.sh`
  - Put `export PATH="$HOME/.venvs/slipstream/bin:$PATH"` at the top of each script.

---

## Task A — Engine (C++ + proto)

**Files:**
- `proto/slipstream/v1/execution.proto`
- `engine/src/{config,router,engine,risk,schedule,twap,vwap,almgren_chriss,service}.{h,cpp}`
- Tests: `engine/tests/*`

Make one commit per sub-part.

### A1. Venue trading rules

- **Flag grammar:**
  - `--venue NAME:fee_bps=F[,min_qty=Q][,qty_step=S][,min_notional=N]`
  - Each value follows the existing strict plain-decimal rule: no sign, no exponent, no whitespace.
  - Bounds:
    - `fee_bps` in [0, 1000]
    - `min_qty` in [0, 1e6]
    - `qty_step` in [0, 1e6], where 0 means no rounding
    - `min_notional` in [0, 1e9]
  - Unknown keys, duplicate keys, a missing `fee_bps`, or an empty value → startup error.
  - Defaults are 0, so existing behaviour and tests are unchanged.
- **Data model:**
  - `VenueConfig` and `VenueSettings` gain `min_qty`, `qty_step` and `min_notional`.
  - `VenueLiquidity` gains the same three fields.
- **Router (`route`):** walk exactly as today, but keep each leg's consumed `(price, take)` pieces in walk order. After the walk, for each leg:
  1. Floor the leg quantity to `qty_step`: `floor(qty / step + 1e-9) * step`. Remove the excess from the leg's last (worst-priced) pieces, and recompute `gross_notional` and `fee` from the remaining pieces.
  2. If the leg quantity is below `min_qty`, or its notional is below `min_notional`, drop the leg.
  3. Totals (`filled_qty`, `gross_notional`, `fees`) are the sums over the surviving legs.

  Quantity from dropped legs is **not** re-routed within the same call; the engine retries it on the next step.
- **Router tests:**
  - A step of 0.01 floors a 0.037 leg to 0.03 and removes the excess from the worst price.
  - A leg below `min_qty` is dropped while the other leg survives.
  - A leg below `min_notional` is dropped.
  - All-zero rules reproduce today's results exactly.
  - The existing 200-trial property test still holds: routed all-in ≤ each available single venue + 1e-9.

### A2. Accumulate children to the venue minimum; residual completion

- Add `min_executable_locked(now_ns, ref_price)`. It returns the smallest `max(min_qty, min_notional / ref_price)` over **fresh** venues that have liquidity on the needed side.
- In `advance_locked`:
  - If `child` is below `min_executable`, and `request.qty - filled_qty` is at least `min_executable`, return. The target keeps accumulating, and the order trades once the child is large enough. This fixes POV's 1e-8 BTC fills.
  - If `request.qty - filled_qty` is below `min_executable`, the remaining quantity can never trade. Set the state to `Completed` and set `halt_reason = "remaining <qty> below venue minimum"`, formatted with `%.8g`.
- Keep `kDustFraction` for float noise only.
- **Tests:**
  - A POV order with `min_qty` 0.00005 and 1e-6 prints produces no fills until the accumulated target reaches 0.00005, then produces one fill of at least 0.00005.
  - A TWAP order whose last slice leaves 0.00001 after flooring completes with that `halt_reason`.

### A3. Risk check on the routed result, including fees; price collar

- New flag `--max-deviation-bps` (default 50, range (0, 10000], plain decimal).
- `liquidity_locked(side, now_ns, only, ref_price)` drops levels beyond the collar before routing:
  - buy: price > `ref × (1 + c)`
  - sell: price < `ref × (1 − c)`
  - where `c = bps / 1e4`
- `advance_locked` order becomes:
  1. Compute the child.
  2. Route it.
  3. If `result.filled_qty <= 0`, return.
  4. Run the risk check with the **routed** notional: `check_child(side, result.filled_qty, result.gross_notional + result.fees, position_, order.filled_notional + order.fees)`.
  5. Commit.
- `RiskCheck::check_child` signature changes to `(side, qty, fill_cost, current_position, spent_cost)`. It checks `spent_cost + fill_cost <= max_order_notional`, plus the position check.
- `check_parent` keeps its `qty × mid` pre-trade estimate.
- Counterfactual single-venue routes use the same collar.
- **Tests:**
  - With `max_order_notional` 1000, spent 990, and a child whose mid estimate is 8 but whose route costs 12 (book walk plus fee), the order is **halted with "order notional limit exceeded" and no fill is committed**.
  - A level 60 bps from mid is excluded, and a level 40 bps from mid is used.
  - A sell collar test mirrors the buy one.

### A4. Every schedule expires

- Make `Schedule::expired(now_ns)` non-virtual in effect: every schedule returns `now_ns >= start_ns && now_ns - start_ns >= duration_ns`.
  - The simplest way is to move the `SliceParams` accessor into the base class or give each schedule the same implementation. POV already does this.
  - Check each schedule's last slice time and confirm it is `< duration`.
- **Test:** for TWAP, VWAP and Almgren-Chriss, with no liquidity, a step at `start + duration` halts the order with "deadline reached".

### A5. Proto and service additions (all additive)

- `StepReply.working_orders` (int32 = 2): the number of orders still `Working` after the step.
- `StatusReply.book_depth` (int32 = 6).
- `VenueInfo.min_qty = 3`, `qty_step = 4`, `min_notional = 5`.
- `OrderStatus.immediate_filled_qty = 16`: how much the one-shot benchmark route could fill, under the same collar and rules.
- **Service test:** each new field is populated.

**Gate:** `bash scripts/build_engine.sh && ctest --test-dir build/engine --output-on-failure` all pass. Then `bash scripts/gen_proto.sh`, and the Python tests still pass.

---

## Task B — Venue rules fetch and release build (Python + scripts)

**Files:**
- Create `python/slipstream/venue_rules.py`
- Modify `python/slipstream/cli.py` (new `venue-flags` subcommand only)
- `scripts/build_release.sh` (new)
- `scripts/demo_live.sh`, `scripts/demo_compare.sh`
- Tests: `python/tests/test_venue_rules.py`, `test_cli.py`

### B1. `venue_rules.py`

- `@dataclass(frozen=True) VenueRules(min_qty: float, qty_step: float, min_notional: float)`
- `class VenueRulesError(MarketDataError)`
- `parse_kraken_rules(raw: bytes, pair_key="XXBTZUSD") -> VenueRules`
  - Requires `error == []`.
  - Takes `result[pair_key]`, which must be the only key.
  - `ordermin` and `costmin` are decimal strings.
  - `lot_decimals` is an int in 0..12, and the step is `10 ** -lot_decimals`.
- `parse_coinbase_rules(raw: bytes, product="BTC-USD") -> VenueRules`
  - `product_id` must match.
  - `status == "online"` and `trading_disabled is False`; otherwise raise ("venue not trading").
  - `base_min_size`, `base_increment` and `quote_min_size` are decimal strings.
- **Validation:**
  - Strict decimal strings of at most 64 chars.
  - Finite and ≥ 0.
  - Response size capped at 1 MiB.
  - Invalid JSON, deep nesting and huge ints all raise `VenueRulesError` only. Use the existing hostile-input pattern in `kraken.py` and `coinbase.py`.
- `fetch_venue_rules(venues, symbol, fetch=_http_get) -> dict[Venue, VenueRules]`
  - Uses fixed https URLs (table above) only.
  - Reuses `kraken_rest.NO_REDIRECT_OPENER` and its timeout and size-cap approach.
  - Symbol mapping: `BTC/USD` maps to `XBTUSD` / `XXBTZUSD` and to `BTC-USD`. Any other symbol raises.
- **Tests:**
  - Both parsers against the real payload shapes in the table above (build fixtures from those fields).
  - A hostile-input matrix.
  - `trading_disabled` true raises.
  - `fetch` with an injected fake is called with the exact URLs.

### B2. `slipstream venue-flags`

- Arguments:
  - `--venues kraken,coinbase` (existing `_venues` parser)
  - `--fees kraken=40,coinbase=60` (strict: every listed venue needs a fee, each in [0, 1000])
  - `--symbol BTC/USD`
- Prints engine arguments **one per line**, for example:
  - `--venue`
  - `kraken:fee_bps=40,min_qty=0.00005,qty_step=1e-08,min_notional=0.5`

  Format numbers with `repr` → plain decimal: use `format(x, "f").rstrip("0").rstrip(".")` or `"0"` so the engine's strict parser accepts them (no exponent).
- Exit 1 with a logged error on `VenueRulesError` or `OSError`.
- **Test:** the output with a monkeypatched `fetch_venue_rules`.

### B3. Release build and demo scripts

- `scripts/build_release.sh`: `cmake -S engine -B build/release -G Ninja -DCMAKE_BUILD_TYPE=Release -DSLIPSTREAM_SANITIZE=OFF && cmake --build build/release`.
- The demo scripts:
  - Build the release engine if `build/release/slipstream_engine` is missing.
  - Fetch the rules: `mapfile -t VENUE_ARGS < <(python -m slipstream.cli venue-flags --venues kraken,coinbase --fees "kraken=${KRAKEN_FEE_BPS:-40},coinbase=${COINBASE_FEE_BPS:-60}")`. Abort if it fails; `set -euo pipefail` plus a check that the array is non-empty.
  - Start `build/release/slipstream_engine ... "${VENUE_ARGS[@]}"`.
- CI keeps Debug + ASan (`scripts/ci.sh` unchanged).

---

## Task C — Recorder correctness (Python)

**Files:** `python/slipstream/recorder.py`, `python/tests/test_recorder.py`

- **Stamping:** stamp `recv_ns` immediately after `ws.recv()` returns, before validation.
- **Window:** the recording window starts when **every** feed has delivered its first message. Use an `asyncio.Event` barrier plus a per-feed first-message flag.
  - Messages that arrive before the barrier are still written; they include the snapshots.
  - `duration_s` counts from the barrier.
  - If a feed never delivers within `IDLE_TIMEOUT_S`, raise the existing idle `RecordError`.
- **Write each message once, off the event loop:**
  - A single writer thread consumes a `queue.Queue` in order. On exit, flush it and join the thread, and surface any write error as `RecordError`.
  - Write the raw message text verbatim when it contains no `\n` or `\r`: `'{"recv_ns": %d, "venue": "%s", "msg": ' + raw + '}'`, where venue comes from the allowlist and raw has already been validated as JSON by the parser. Otherwise fall back to `json.dumps(json.loads(raw))`.
  - This avoids the second parse and re-encode, and keeps the exchange's decimal strings for a later checksum.
  - `bytes` messages are decoded as UTF-8; decode errors raise `RecordError`.
- **Tests:**
  - The window starts at the barrier: with a fake clock and a fake server that delays feed 2 by X, the recording covers ≥ `duration_s` after both feeds deliver.
  - A raw line is written verbatim and round-trips through `read_replay`.
  - A message with an embedded newline is re-encoded.
  - A write failure surfaces as `RecordError`.

---

## Task D — Python integration (after A is merged in; B's CLI changes first)

**Files:** `python/slipstream/{engine_client,runner,cli,live,replay}.py`, `python/tests/conftest.py`, tests

- **D1. `EngineClient.step` returns `StepResult(fills: list[Fill], working_orders: int)`.**
  - The runner stores `self._working`.
  - `is_done()` is now `self._submitted and self._working == 0`, with no RPC.
  - `order_statuses()` still calls `GetStatus`, but only for the final summary.
  - Update `FakeEngine.step` to match.
- **D2. The runner stops updating local books once submitted.** They are used only for calibration.
- **D3. CLI:**
  - Read the engine's `book_depth` and refuse to run when it is greater than `--depth` for live and compare. Reason: Kraken never deletes levels outside the subscribed depth, so they would become phantom liquidity.
  - Show `saved` as `n/a` unless `filled_qty ≥ total_qty × (1 − 1e-9)` **and** `immediate_filled_qty ≥ total_qty × (1 − 1e-9)`. Format it with `_num` to avoid `-0.00`.
  - Add a `filled %` column to the comparison table and a matching line in the summary.
  - Show `halt_reason` in the comparison table as a trailing column when any order has one.
  - When a live or replay run fails **after** submission, still print the summary or table of what was filled, then exit 1.
- **D4. End-to-end:**
  - Update `test_integration_routing` (all-zero rules, so the numbers are unchanged).
  - Add an end-to-end test with Kraken `min_qty=0.00005`: a POV order in a replay fixture made of tiny trades only fills in chunks ≥ 0.00005.

**Gate:** `bash scripts/ci.sh` → `CI OK`.

---

## Task E — Verification

1. **Offline:** replay the two-venue fixture with the release engine and check that the output tables are well-formed.
2. **Live demo:** `show.sh 60` in the user's terminal. Check that:
   - no fill is below 0.00005 BTC on Kraken or $1 on Coinbase;
   - every order ends COMPLETED or HALTED with a reason;
   - no `-0.00` appears;
   - `saved` shows `n/a` for partial fills;
   - the startup line shows the venue rules.
3. Update `note.md` (dev log and known limitations) and the README table explanation.
4. Push, open the PR, merge when green (standing approval).
