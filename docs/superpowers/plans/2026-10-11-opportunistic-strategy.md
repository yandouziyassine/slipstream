# Opportunistic Execution Strategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fifth schedule, `opportunistic`, that trades up to two clips ahead of TWAP when trading is cheap and behind when it is expensive, and always finishes by the deadline.

**Spec:** `docs/superpowers/specs/2026-10-11-opportunistic-strategy-design.md` (approved 2026-10-11). Read it first.

**Architecture:** The engine prices one hypothetical clip per step for each opportunistic order (existing `liquidity_locked` + `route` path, after the order's own consumption, fees included), keeps a bounded per-order history of those costs, and passes the current cost's mid-rank to a pure `OpportunisticSchedule` through `MarketState`. The proto, Python model, CLI, collector, DB and site then carry the new algorithm and its parameters.

**Tech stack:** C++20 engine (gtest, ASan+UBSan, TSan subset, clang-tidy in CI), protobuf/gRPC, Python 3.12 (pytest, ruff, mypy --strict).

**Rules for every task:** `CLAUDE.md` plus the shared agent rules (noreply commit identity via env vars — a local hook refuses anything else; TDD; targeted tests while working, full `bash scripts/ci.sh` once per PR; no comments unless the why is non-obvious; clang-tidy clean: no implicit int→double narrowing, no unchecked optionals in src).

## Tracks and ownership (concurrent work exists)

| Track | Starts | Owns | Must not touch |
|---|---|---|---|
| **E: engine** (tasks E1–E5) | now, from `main` | `engine/src/opportunistic.*`, `engine/src/cost_history.*`, `engine/src/schedule.h`, `engine/src/schedule_spec.h`, `engine/src/schedule_factory.*`, `engine/src/engine.*`, `engine/src/service.cpp`, `engine/CMakeLists.txt`, `engine/tests/*`, `proto/slipstream/v1/execution.proto` (new message + `ParentOrder` field 11 only) | all Python |
| **P: Python** (tasks P1–P4) | after the open branches that own `engine_stream.py`/`session.py` (latency), `collect.py`/`storage.py` (cloud wave 2) and migration 004 (method accuracy) are merged | `python/slipstream/models.py`, `engine_stream.py` (`order_to_proto` only), `cli.py`, `collect.py` (`ALGOS`, params), `db/migrations/00N_*.sql`, `db/results_db.py`, `site/*`, their tests | engine |

The method-accuracy branch also edits `engine/src/engine.cpp` (one-shot fee) and `proto` (`OrderStatus` field 17). Track E rebases on whichever lands first; the overlaps are in different functions and different messages.

---

## Track E — engine

### Task E1: pure `OpportunisticSchedule`

**Files:**
- Modify: `engine/src/schedule.h` (`MarketState`)
- Modify: `engine/src/schedule_spec.h` (`OpportunisticSpec`)
- Create: `engine/src/opportunistic.h`, `engine/src/opportunistic.cpp`
- Modify: `engine/src/schedule_factory.cpp`, `engine/CMakeLists.txt`
- Test: `engine/tests/opportunistic_test.cpp`

- [ ] **Step 1: Extend `MarketState` and `ScheduleSpec`.**

```cpp
// schedule.h
struct MarketState {
    double cumulative_volume;
    std::optional<double> cost_rank;  // opportunistic only: mid-rank of the current clip cost
    std::int32_t cost_samples = 0;
};
```
```cpp
// schedule_spec.h
struct OpportunisticSpec {
    double band_slices;
    double cheap_rank;
    double expensive_rank;
};
using ScheduleSpec = std::variant<TwapSpec, VwapSpec, AlmgrenChrissSpec, PovSpec, OpportunisticSpec>;
```
Update every `MarketState{...}` construction in `engine.cpp` to `MarketState{market_volume_, std::nullopt, 0}` (behaviour unchanged).

- [ ] **Step 2: Write the failing schedule tests** (`opportunistic_test.cpp`, register in CMake):

```cpp
#include <gtest/gtest.h>
#include "opportunistic.h"
#include "twap.h"

namespace slipstream {
namespace {
constexpr std::int64_t kSec = 1'000'000'000;
const SliceParams kParams{10.0, 0, 600 * kSec, 10};  // clip = 1.0
const OpportunisticSpec kSpec{2.0, 0.30, 0.70};

double twap_at(std::int64_t t) {
    const auto twap = TwapSchedule::create(kParams);
    return twap->target_qty_at(t, MarketState{0.0, std::nullopt, 0});
}
MarketState ranked(double rank, std::int32_t samples) { return {0.0, rank, samples}; }

TEST(OpportunisticTest, FollowsTwapDuringWarmup) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    EXPECT_DOUBLE_EQ(s.target_qty_at(30 * kSec, ranked(0.0, 100)), twap_at(30 * kSec));  // < 10% elapsed
    EXPECT_DOUBLE_EQ(s.target_qty_at(120 * kSec, ranked(0.0, 19)), twap_at(120 * kSec));  // < 20 samples
}
TEST(OpportunisticTest, CheapRunsAheadByTheBand) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    EXPECT_DOUBLE_EQ(s.target_qty_at(120 * kSec, ranked(0.30, 50)), twap_at(120 * kSec) + 2.0);
}
TEST(OpportunisticTest, ExpensiveFallsBehindByTheBand) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    EXPECT_DOUBLE_EQ(s.target_qty_at(240 * kSec, ranked(0.70, 50)), twap_at(240 * kSec) - 2.0);
}
TEST(OpportunisticTest, NeutralOrUnknownFollowsTwap) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    EXPECT_DOUBLE_EQ(s.target_qty_at(240 * kSec, ranked(0.5, 50)), twap_at(240 * kSec));
    EXPECT_DOUBLE_EQ(s.target_qty_at(240 * kSec, MarketState{0.0, std::nullopt, 50}), twap_at(240 * kSec));
}
TEST(OpportunisticTest, BandShrinksOverTheLastFifthAndEndsOnTheFullQuantity) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    // 540 s: 60 s left of a 120 s shrink window -> half band.
    EXPECT_DOUBLE_EQ(s.target_qty_at(540 * kSec, ranked(0.0, 50)), twap_at(540 * kSec) + 1.0);
    EXPECT_DOUBLE_EQ(s.target_qty_at(600 * kSec, ranked(0.0, 50)), 10.0);
    EXPECT_DOUBLE_EQ(s.target_qty_at(600 * kSec, ranked(1.0, 50)), 10.0);
}
TEST(OpportunisticTest, ClampsToZeroAndTotal) {
    const auto s = OpportunisticSchedule::create(kParams, kSpec).value();
    EXPECT_GE(s.target_qty_at(61 * kSec, ranked(1.0, 50)), 0.0);
    EXPECT_LE(s.target_qty_at(500 * kSec, ranked(0.0, 50)), 10.0);
}
TEST(OpportunisticTest, RejectsInvalidParameters) {
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {0.0, 0.3, 0.7}));
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {10.5, 0.3, 0.7}));
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {2.0, 0.7, 0.3}));
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {2.0, 0.0, 0.7}));
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {2.0, 0.3, 1.0}));
    EXPECT_FALSE(OpportunisticSchedule::create(kParams, {std::nan(""), 0.3, 0.7}));
}
}  // namespace
}  // namespace slipstream
```
Check `twap.h`'s actual factory signature first and adapt `twap_at` to it (keep the test meaning).

- [ ] **Step 3: Run and confirm failure** (`bash scripts/build_engine.sh` fails to compile: no `opportunistic.h`).

- [ ] **Step 4: Implement.**

```cpp
// opportunistic.h
#pragma once
#include <optional>
#include "schedule.h"
#include "schedule_spec.h"
#include "slicing.h"
#include "twap.h"

namespace slipstream {

class OpportunisticSchedule final : public Schedule {
public:
    static constexpr double kMaxBandSlices = 10.0;
    static constexpr double kWarmupFraction = 0.10;
    static constexpr std::int32_t kWarmupSamples = 20;
    static constexpr double kShrinkFraction = 0.20;

    static std::optional<OpportunisticSchedule> create(const SliceParams& params,
                                                       const OpportunisticSpec& spec);
    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "opportunistic"; }

private:
    OpportunisticSchedule(const SliceParams& params, const OpportunisticSpec& spec, TwapSchedule twap);
    OpportunisticSpec spec_;
    TwapSchedule twap_;
};

}  // namespace slipstream
```
```cpp
// opportunistic.cpp — target logic (spec §3.3)
double OpportunisticSchedule::target_qty_at(std::int64_t now_ns, const MarketState& market) const {
    const auto& p = slice_params();
    const double base = twap_.target_qty_at(now_ns, market);
    const double elapsed = static_cast<double>(now_ns - p.start_ns);
    const double duration = static_cast<double>(p.duration_ns);
    const bool warm = elapsed >= kWarmupFraction * duration && market.cost_samples >= kWarmupSamples;
    if (!warm || !market.cost_rank) return base;
    const double clip = p.total_qty / static_cast<double>(p.num_slices);
    const double remaining = std::max(0.0, duration - elapsed);
    const double band = spec_.band_slices * clip * std::min(1.0, remaining / (kShrinkFraction * duration));
    double target = base;
    if (*market.cost_rank <= spec_.cheap_rank) target = base + band;
    else if (*market.cost_rank >= spec_.expensive_rank) target = base - band;
    return std::clamp(target, 0.0, p.total_qty);
}
```
`create` validates: `valid_slice_params(params)`, `std::isfinite` and `0 < band_slices <= kMaxBandSlices`, `0 < cheap_rank < expensive_rank < 1`. Add the `OpportunisticSpec` branch to `make_schedule` in `schedule_factory.cpp` (visit pattern used for the other specs).

- [ ] **Step 5: Run `ctest --test-dir build/engine -R Opportunistic` → PASS; full engine tests still pass.**
- [ ] **Step 6: Commit** `feat(engine): add the pure opportunistic schedule`.

### Task E2: bounded cost history with mid-rank

**Files:** Create `engine/src/cost_history.h`, `engine/src/cost_history.cpp`; Test `engine/tests/cost_history_test.cpp`; register in CMake.

- [ ] **Step 1: Failing tests.**

```cpp
TEST(CostHistoryTest, KeepsAtMostOneSamplePerSecond) {
    CostHistory h;
    h.add(0, 1.0); h.add(500'000'000, 2.0); h.add(1'000'000'000, 3.0);
    EXPECT_EQ(h.size(), 2u);
}
TEST(CostHistoryTest, CapsAtTheLimitDroppingTheOldest) {
    CostHistory h;
    for (std::int64_t i = 0; i < 700; ++i) h.add(i * 1'000'000'000, static_cast<double>(i));
    EXPECT_EQ(h.size(), CostHistory::kCapacity);  // 600
    EXPECT_DOUBLE_EQ(h.rank(99.0), 0.0);          // 0..99 were dropped
}
TEST(CostHistoryTest, MidRankCountsTiesAsHalf) {
    CostHistory h;
    for (std::int64_t i = 0; i < 4; ++i) h.add(i * 1'000'000'000, 5.0);
    EXPECT_DOUBLE_EQ(h.rank(5.0), 0.5);
    EXPECT_DOUBLE_EQ(h.rank(1.0), 0.0);
    EXPECT_DOUBLE_EQ(h.rank(9.0), 1.0);
}
TEST(CostHistoryTest, ClearReleasesEverything) {
    CostHistory h; h.add(0, 1.0); h.clear();
    EXPECT_EQ(h.size(), 0u);
}
TEST(CostHistoryTest, RankOfEmptyHistoryIsUnknown) {
    EXPECT_FALSE(CostHistory{}.rank_or_none(1.0));
}
```
- [ ] **Step 2: Fail, implement** a `std::deque<double>` (or fixed ring) plus `last_ns_`; `add` ignores samples within 1 s of the last kept one and non-finite costs; `rank(c) = (count(< c) + 0.5 * count(== c)) / size()`; `rank_or_none` returns `std::nullopt` when empty; `kCapacity = 600`; `clear()` swaps with an empty container.
- [ ] **Step 3: Pass; commit** `feat(engine): add a bounded clip-cost history with mid-rank`.

### Task E3: engine prices the clip and feeds the rank

**Files:** Modify `engine/src/engine.h`, `engine/src/engine.cpp`; Test `engine/tests/engine_opportunistic_test.cpp` (register in CMake).

- [ ] **Step 1: Failing tests** (use the patterns and helpers of `engine_consumption_test.cpp` and `engine_test.cpp`):
  - `ClipCostMatchesAHandComputedSweepIncludingFees`: one venue, a known book and fee; after a step the order's latest recorded cost equals `(avg_price × (1 + fee) − mid) / mid × 1e4` for a buy of one clip (expose a test accessor `Engine::latest_clip_cost(order_id)` returning `std::optional<double>`).
  - `PricingTheClipDoesNotFillOrConsume`: `consumption_entries()` and fills are identical to a TWAP order's on the same book except for the opportunistic schedule's own target.
  - `UnpriceableClipRecordsNoSample`: with no usable market the sample count does not grow.
  - `FillsAheadOfTwapWhenCheap`: replay a book whose spread tightens sharply after warm-up; the opportunistic order's filled quantity exceeds a TWAP order's at the same time by up to 2 clips and never more.
  - `FallsBehindWhenExpensive`: the opposite.
  - `CompletesByTheDeadline`: filled quantity equals the order quantity at the deadline when liquidity allows.
  - `HistoryIsFreedOnCompletionAndHalt`: a new accessor `Engine::cost_history_samples()` (total across orders) returns 0 after completion, deadline halt and risk halt.
  - `OtherSchedulesAreUnaffected`: TWAP/VWAP/POV/AC results identical to before (existing tests keep passing).
- [ ] **Step 2: Implement.** `ParentOrder` gains `CostHistory costs;`. In `Engine::step`, before computing an opportunistic order's target:
  1. `const double clip = order.request.qty / order.request.num_slices;`
  2. `auto liquidity = liquidity_locked(side, now_ns, std::nullopt, *ref_price, order.taken);`
  3. `const auto priced = route(side, std::min(clip, remaining), liquidity);` — if `priced.filled_qty` is below the clip size (minimums, collar, no market) record nothing; otherwise `cost = side_sign * (all_in_notional(side, priced) / priced.filled_qty - mid) / mid * 1e4` and `order.costs.add(now_ns, cost)`.
  4. Build `MarketState{market_volume_, order.costs.rank_or_none(cost), static_cast<std::int32_t>(order.costs.size())}` for that order only.
  Do not call `record` on the overlay for the priced clip. Release `order.costs.clear()` wherever `release_taken(order)` runs.
- [ ] **Step 3: Pass all engine tests (ASan+UBSan); commit** `feat(engine): price a clip per step for opportunistic orders`.

### Task E4: protocol and service validation

**Files:** Modify `proto/slipstream/v1/execution.proto`, `engine/src/service.cpp`; Test `engine/tests/service_test.cpp`.

- [ ] **Step 1: Proto.**
```proto
message OpportunisticParams {
  double band_slices = 1;
  double cheap_rank = 2;
  double expensive_rank = 3;
}
// in ParentOrder.oneof schedule:
    OpportunisticParams opportunistic = 11;
```
- [ ] **Step 2: Failing service tests:** a valid opportunistic submit is accepted and `GetStatus` reports `algo == "opportunistic"`; band 0, band 10.5, NaN, `cheap_rank >= expensive_rank`, ranks outside (0,1) are rejected with `INVALID_ARGUMENT` (match how other schedule params are rejected — read `service.cpp`).
- [ ] **Step 3: Map the oneof to `OpportunisticSpec` in `service.cpp`; pass; commit** `feat(proto): accept opportunistic orders over gRPC`.

### Task E5: latency check and PR

- [ ] Run `bash scripts/bench_pipeline.sh` three times before (on `main`) and after; report engine p50/p99 (median of runs). A material regression (> 1 power-of-two bucket on the median) blocks the PR.
- [ ] Full `bash scripts/ci.sh`; open the PR (controller pushes).

---

## Track P — Python (starts after the latency, cloud wave 2 and method-accuracy branches are merged)

### Task P1: model, proto mapping and CLI

**Files:** `python/slipstream/models.py`, `python/slipstream/engine_stream.py` (`order_to_proto` only), `python/slipstream/cli.py`; tests `python/tests/test_models.py`, `test_engine_stream.py`, `test_cli.py`.

- [ ] Failing tests: `Algo` includes `"opportunistic"`; `OrderSpec(..., algo="opportunistic")` carries `OpportunisticParams(band_slices=2.0, cheap_rank=0.3, expensive_rank=0.7)` by default; `order_to_proto` sets `order.opportunistic.*`; CLI `--algo opportunistic --band-slices 1.5 --cheap-rank 0.25 --expensive-rank 0.75` parses and rejects invalid values (same rules as E4) at the CLI boundary.
- [ ] Implement with a frozen dataclass `OpportunisticParams` next to `PovParams`, and an `isinstance` branch in `order_to_proto` mirroring the others.
- [ ] Commit `feat: run opportunistic orders from Python and the CLI`.

### Task P2: store each algorithm's parameters

**Files:** `python/slipstream/db/migrations/00N_results_params.sql` (N = next free number on main), `python/slipstream/db/results_db.py`, `python/tests/test_results_db*.py`.

- [ ] Failing tests: the migration adds nullable `params_json TEXT` to `results` (append-only triggers unchanged); `add_results(..., params_by_order_id={...})` stores canonical JSON (`sort_keys=True`); rows without params store NULL; version expectations updated.
- [ ] Implement; commit `feat(db): store each result's algorithm parameters`.

### Task P3: collector runs it hourly

**Files:** `python/slipstream/collect.py`, `python/tests/test_collect.py`.

- [ ] Failing tests: `ALGOS` ends with `"opportunistic"`; `_order_specs` builds five specs per size; the position-limit rule `HOURLY_MAX_POSITION >= len(ALGOS) * max(sizes)` still holds (1.5 ≥ 1.25); `params_json` is stored for opportunistic and POV rows.
- [ ] Implement; commit `feat(collect): add the opportunistic strategy to the hourly runs`.

### Task P4: site and replay integration

**Files:** `python/slipstream/site/render.py` (+ templates, snapshot fixture), `python/tests/test_integration_schedules.py` (or a new `test_integration_opportunistic.py`).

- [ ] Failing tests: the site lists "Opportunistic" (label `OPP`) wherever the four algorithms appear; the paired comparison against TWAP renders; a replay integration test on a fixture with a tightening spread shows the opportunistic order ahead of TWAP mid-window, within ±2 clips, and complete at the end; two replays give identical results.
- [ ] Implement; regenerate the snapshot (diff only the intended additions); commit `feat(site): show the opportunistic strategy`.
- [ ] Full `bash scripts/ci.sh`; docs text for README/note.md to the controller.

---

## Self-review against the spec
- §3.1 clip cost → E3; §3.2 history and mid-rank → E2/E3; §3.3 pure schedule (warm-up, band, shrink, clamp) → E1; §3.4 proto/validation → E4, Python/CLI → P1, collector → P3, params storage → P2, site/stats → P4; §4 safety (no lookahead, bounded memory, latency check) → E2/E3/E5; §5 tests → each task; §6 evaluation uses the existing paired bootstrap → P4.
