# Opportunistic Execution Strategy — Design

Date: 2026-10-11
Status: Approved by the user in conversation (approach 1, "cost-aware band around TWAP").
Scope: a fifth execution schedule next to TWAP, VWAP, POV and Almgren-Chriss.

## 1. Purpose

The user wants Slipstream to "buy or sell whenever the market is at its best", meaning **cheapest to trade**: pay as little as possible to fill the order, fees included. The strategy trades ahead of a steady pace when trading is cheap right now and falls behind when it is expensive, and always finishes the order by its deadline.

It reacts only to the visible cost of trading now. It does not predict where the price is going (rejected option "best price level": it needs a forecasting model, and every result in this project avoids prediction claims).

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Signal | **Clip cost**: the all-in cost in bps of trading one clip (one normal slice, `qty / num_slices`) right now, measured against the **current** consolidated mid | Captures spread, depth for our size, exchange fees and this order's own consumed liquidity in one number. Measuring against the current mid (not the arrival mid) isolates how cheap trading is, not how the price drifted. |
| What counts as cheap | The clip cost's percentile rank among the costs this order has observed so far: rank ≤ 0.30 is cheap, rank ≥ 0.70 is expensive | Self-calibrating: no fixed bps threshold that is wrong on quiet or busy days. Uses only past and current data (no lookahead). |
| How far it may deviate | A band of **2 clips** ahead of or behind the TWAP target | Bounds the extra risk and keeps the comparison with TWAP fair. |
| Warm-up | Follow TWAP exactly until 10% of the window has passed **and** at least 20 cost samples exist | A percentile needs history. |
| Completion | Over the last 20% of the window the band shrinks linearly to 0 | The target converges to TWAP's, which reaches the full quantity at the deadline: no large last-second trade, nothing left unfilled by design. |
| Sampling | At most one cost sample per second per order; keep the most recent 600 | Bounds memory (600 doubles per order) and per-step work. A 10-minute order fills it exactly. |
| Parameters | `band_slices = 2`, `cheap_rank = 0.30`, `expensive_rank = 0.70` are explicit, validated, sent with each order, and stored with every run | Reproducible; tunable later without code changes. |

## 3. Design

### 3.1 Clip cost (engine)

At each engine step, for each Working opportunistic order, the engine prices a hypothetical clip with the existing router path, on fresh venues, after the order's own consumption overlay, inside the price collar, with each venue's taker fee, exactly as a real child of this order would be priced:

`clip_cost_bps = side_sign × (avg_price_incl_fees − current_mid) / current_mid × 10⁴`

If the clip cannot be priced (no usable market, the collar cuts it, or venue minimums block it), no sample is recorded for that step and the rank is "unknown". The strategy then follows TWAP for that step. Nothing is guessed.

Pricing the clip does not fill anything and does not change the consumption overlay.

### 3.2 Cost history and rank (engine)

Each opportunistic `ParentOrder` holds a bounded ring of cost samples (at most one per second of engine time, at most 600). The rank of the current sample is the fraction of samples in the ring that are strictly cheaper, plus half of the ties. Mid-rank keeps a flat market at 0.5, which is "neutral".

The engine passes the rank to the schedule through `MarketState`, which gains `std::optional<double> cost_rank` (unset for the other schedules and when unknown). The ring is freed when the order completes or halts, like the consumption overlays.

### 3.3 Schedule (engine, pure)

`OpportunisticSchedule` (`engine/src/opportunistic.{h,cpp}`) implements `target_qty_at(now, market)`:

1. `twap(t)`: the same cumulative target as `TwapSchedule` for these `SliceParams`.
2. `clip = total_qty / num_slices`.
3. `band(t) = band_slices × clip × min(1, remaining_time / (0.2 × duration))`.
4. During warm-up, or when `cost_rank` is unknown: `target = twap(t)`.
5. Cheap (`rank ≤ cheap_rank`): `target = twap(t) + band(t)`. Expensive (`rank ≥ expensive_rank`): `target = twap(t) − band(t)`. Otherwise: `twap(t)`.
6. Clamp to `[0, total_qty]`.

The schedule holds no mutable state. The engine owns the history and passes the rank in, so the schedule stays a pure function that is easy to test. Warm-up needs the sample count, so `MarketState` also carries `cost_samples` (an integer).

The engine already fills toward the cumulative target and never sells back, so being behind and then "cheap" naturally catches up, and a target below what is already filled simply pauses the order.

### 3.4 Protocol and Python

- Proto: `message OpportunisticParams { double band_slices = 1; double cheap_rank = 2; double expensive_rank = 3; }`, and `OpportunisticParams opportunistic = 11;` in `ParentOrder.schedule`.
- Engine validation at the gRPC boundary: `band_slices` finite, in (0, 10]; `0 < cheap_rank < expensive_rank < 1`; otherwise the submit is rejected.
- Python: `Algo` gains `"opportunistic"`; `OrderSpec` and `order_to_proto` carry the parameters with the defaults above; CLI `--algo opportunistic` with `--band-slices`, `--cheap-rank`, `--expensive-rank`.
- Collector: `ALGOS` gains `"opportunistic"` as the fifth algorithm in every hourly run. The position limit stays 1.5 BTC per engine (5 × 0.25 = 1.25). Each result row stores its algorithm's parameters in a new nullable `params_json TEXT` column on `results` (a local migration numbered after the latest one on main at implementation time; append-only rules unchanged). The opportunistic row stores `{"band_slices": 2, "cheap_rank": 0.3, "expensive_rank": 0.7}`, and other algorithms store their own parameters (for example POV's participation) or NULL.
- Site and stats: the new algorithm appears in the tables and charts like the others; its comparison with TWAP uses the paired bootstrap within the same runs.

## 4. Safety

- Every existing protection applies unchanged: risk limits before any fill, the 50 bps price collar, venue minimums, liquidity consumption, the 30 s stale-market halt, the deadline halt.
- No lookahead: the rank uses only samples at or before the current step.
- Bounded memory and work: 600 samples per order; one extra router pricing per opportunistic order per step. Engine latency is measured before and after with `scripts/bench_pipeline.sh`; a material regression blocks the merge.
- Deterministic under the replay clock (same recording, same results).

## 5. Testing

- Schedule (pure): warm-up equals TWAP; cheap → `twap + band`; expensive → `twap − band`; neutral → `twap`; unknown rank → `twap`; the band shrinks to 0 over the last 20%; the target equals `total_qty` at the deadline; clamps at 0 and `total_qty`.
- Engine: clip cost equals a hand-computed sweep incl. fees and own consumption; pricing does not fill or consume; at most one sample per second; the ring is capped at 600 and freed on completion and halt; the rank uses mid-rank ties; an unpriceable clip records no sample.
- Integration (replay): on a fixture where cost varies, the opportunistic order fills more during cheap stretches than TWAP and stays within ±2 clips of TWAP; it always completes when the market allows; results are identical across two replays.
- Boundary: invalid parameters are rejected at submit.

## 6. Evaluation

Compared with TWAP within the same runs (paired bootstrap). It is called "better" only when the confidence interval of the difference in all-in bps excludes 0, with n reported. If it does not beat TWAP, that is reported as a result.

## 7. Out of scope

Price prediction; limit orders or passive (maker) fills; adapting the band during an order; cross-order learning.
