# Smart Routing — PR 1 (Engine) + PR 2 (Python venues) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- **Engine:** register several venues, each with its own taker fee. Route every child slice across the fresh venues by fee-adjusted price, and report each order's routed all-in cost next to what it would have cost on each venue alone.
- **Python:** parse Coinbase Advanced Trade market data strictly, tag every message with its venue, run several WebSocket feeds concurrently, and record and replay multi-venue sessions.

**Architecture:**
- **Engine:** holds one `OrderBook` per venue, with a caller-supplied `recv_ns` per update. It builds the consolidated mid from the fresh books and skips crossed books. A pure `route()` function walks the merged, fee-adjusted liquidity. Counterfactual prices reuse `route()` on one venue at a time.
- **Python:** each venue has its own parser. Coinbase's parser is stateful, because it tracks sequence numbers per connection. The runner dispatches each message to the right parser by venue.
- **PR 3** (separate plan, written after PRs 1 and 2 merge) wires the venue and `recv_ns` into the gRPC client, and adds `--venues`, the new report columns, the two-venue end-to-end test, and the live comparison run.

**Tech Stack:** C++20, gRPC 1.51, GoogleTest, ASan/UBSan · Python 3.12, websockets, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-09-24-smart-routing-design.md`

## Conventions (every task)
- **Engine build/test:** WSL Ubuntu 24.04, run via a script file under the session scratchpad: `export PATH="$HOME/.venvs/slipstream/bin:$PATH"; cd /mnt/c/Users/yando/OneDrive/Documents/Code/Project1/.worktrees/<wt>; bash scripts/build_engine.sh && ctest --test-dir build/engine --output-on-failure`. Run it from Git Bash with `MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash /mnt/c/.../script.sh 2>&1 | tr -d '\0'`. The final gate is `bash scripts/ci.sh`, which must print `CI OK`.
- **Python:** run from `python/` with `C:/Users/yando/.venvs/slipstream/Scripts/python.exe -m pytest -q -p no:cacheprovider` plus mypy/ruff. On Windows, deselect the two real-engine e2e tests (`tests/test_integration_replay.py::test_replay_twap_end_to_end_against_real_engine`, `tests/test_integration_schedules.py::test_four_schedules_end_to_end`).
- **Commits:** `GIT_AUTHOR_EMAIL=160782497+yandouziyassine@users.noreply.github.com GIT_COMMITTER_EMAIL=160782497+yandouziyassine@users.noreply.github.com git commit …`, with a HEREDOC message and a blank line before the `Co-Authored-By` trailer. Never change git config, and never push.
- **C++:** `-Wall -Wextra -Wpedantic -Wshadow -Werror` with ASan/UBSan. Never weaken warnings or tests.
- **After EVERY task,** run `/security-review` and `/caveman:caveman-review` (CLAUDE.md), and fix any valid findings test-first before the next task.

| Track | Branch / worktree | Tasks |
|---|---|---|
| E | `feat/routing-engine` / `.worktrees/routing-engine` | E1 → E4 |
| P | `feat/coinbase-venue` / `.worktrees/coinbase-venue` | P1 → P4 |

---

# Track E — Engine (PR 1)

### Task E1: Venue config flags

**Files:** Modify `engine/src/config.h`, `engine/src/config.cpp`, `engine/tests/config_test.cpp`.

- [ ] **Step 1: Add the failing tests** to `config_test.cpp`:
```cpp
TEST(Config, DefaultsToSingleFeeFreeKraken) {
    const auto result = parse_args({});
    ASSERT_TRUE(result.config);
    ASSERT_EQ(result.config->venues.size(), 1u);
    EXPECT_EQ(result.config->venues[0].name, "kraken");
    EXPECT_DOUBLE_EQ(result.config->venues[0].fee_bps, 0.0);
    EXPECT_EQ(result.config->stale_ns, 2'000'000'000);
}

TEST(Config, ParsesVenuesInOrderAndStaleness) {
    const auto result = parse_args({"--venue", "coinbase:fee_bps=60", "--venue", "kraken:fee_bps=40",
                                    "--stale-ms", "500"});
    ASSERT_TRUE(result.config) << result.error;
    ASSERT_EQ(result.config->venues.size(), 2u);
    EXPECT_EQ(result.config->venues[0].name, "coinbase");
    EXPECT_DOUBLE_EQ(result.config->venues[0].fee_bps, 60.0);
    EXPECT_EQ(result.config->venues[1].name, "kraken");
    EXPECT_DOUBLE_EQ(result.config->venues[1].fee_bps, 40.0);
    EXPECT_EQ(result.config->stale_ns, 500'000'000);
}

TEST(Config, RejectsBadVenues) {
    for (const char* value : {"binance:fee_bps=10", "kraken", "kraken:fee=10", "kraken:fee_bps=",
                              "kraken:fee_bps=-1", "kraken:fee_bps=1001", "kraken:fee_bps=nan",
                              "kraken:fee_bps=10x", ":fee_bps=10"}) {
        EXPECT_FALSE(parse_args({"--venue", value}).config) << value;
    }
    EXPECT_FALSE(parse_args({"--venue", "kraken:fee_bps=1", "--venue", "kraken:fee_bps=2"}).config);
}

TEST(Config, RejectsBadStaleness) {
    for (const char* value : {"0", "-1", "abc", "600001", "1.5"}) {
        EXPECT_FALSE(parse_args({"--stale-ms", value}).config) << value;
    }
}
```

- [ ] **Step 2: Build and confirm the failure** (`venues` / `stale_ns` are not members).

- [ ] **Step 3: Implement**
  - In `config.h`:
    - Add `struct VenueConfig { std::string name; double fee_bps; };`.
    - Add `constexpr std::array<std::string_view, 2> kKnownVenues{"kraken", "coinbase"};` (include `<array>` and `<string_view>`).
    - Add the `EngineConfig` members `std::vector<VenueConfig> venues{{"kraken", 0.0}};` and `std::int64_t stale_ns = 2'000'000'000;`.
  - In `config.cpp`, handle `--venue` and `--stale-ms`:
    - **`--venue`:**
      - On the first `--venue` flag, clear the default venue list.
      - Split the value at the first `':'`. The name must be in `kKnownVenues` and must not be registered already.
      - The rest must start with `fee_bps=`. The number after it must be parsed fully with `std::stod` inside try/catch, must be finite, and must be within [0, 1000].
      - Error messages: `"invalid --venue (name:fee_bps=N, name in kraken|coinbase, 0<=N<=1000)"` and `"duplicate --venue"`.
    - **`--stale-ms`:** all digits (reuse `all_digits`), 1 to 6 characters, value in 1..600000. Store `value * 1'000'000`.

- [ ] **Step 4: Build, run ctest (all pass), commit** `feat(engine): configure venues with taker fees and staleness`. Then run the two reviews.

### Task E2: Router (pure function)

**Files:** Create `engine/src/router.h`, `engine/src/router.cpp`, `engine/tests/router_test.cpp`; add them to CMake.

- [ ] **Step 1: Write the failing test `engine/tests/router_test.cpp`**
```cpp
#include "router.h"

#include <gtest/gtest.h>

#include <random>
#include <vector>

#include "fill_simulator.h"

using namespace slipstream;

TEST(Router, BuyTakesCheapestFeeAdjustedLevelsAcrossVenues) {
    // venue 0: ask 100 with 40 bps fee -> effective 100.4; venue 1: asks 100.3/100.6, no fee.
    const std::vector<VenueLiquidity> venues{{0, 0.004, {{100.0, 1.0}}},
                                             {1, 0.0, {{100.3, 0.5}, {100.6, 5.0}}}};
    const auto result = route(Side::Buy, 1.0, venues);
    ASSERT_EQ(result.legs.size(), 2u);
    EXPECT_EQ(result.legs[0].venue, 0u);
    EXPECT_DOUBLE_EQ(result.legs[0].qty, 0.5);
    EXPECT_DOUBLE_EQ(result.legs[0].gross_notional, 50.0);
    EXPECT_DOUBLE_EQ(result.legs[0].fee, 0.2);
    EXPECT_EQ(result.legs[1].venue, 1u);
    EXPECT_DOUBLE_EQ(result.legs[1].qty, 0.5);
    EXPECT_DOUBLE_EQ(result.legs[1].gross_notional, 50.15);
    EXPECT_DOUBLE_EQ(result.legs[1].fee, 0.0);
    EXPECT_DOUBLE_EQ(result.filled_qty, 1.0);
    EXPECT_DOUBLE_EQ(all_in_notional(Side::Buy, result), 100.35);
}

TEST(Router, SellTakesHighestFeeAdjustedBids) {
    // venue 0 bid 100 fee 40 bps -> 99.6; venue 1 bid 99.7 no fee -> 99.7 first.
    const std::vector<VenueLiquidity> venues{{0, 0.004, {{100.0, 1.0}}}, {1, 0.0, {{99.7, 0.4}}}};
    const auto result = route(Side::Sell, 1.0, venues);
    ASSERT_EQ(result.legs.size(), 2u);
    EXPECT_DOUBLE_EQ(result.legs[1].qty, 0.4);
    EXPECT_DOUBLE_EQ(result.legs[0].qty, 0.6);
    EXPECT_DOUBLE_EQ(all_in_notional(Side::Sell, result), 0.6 * 100.0 * (1 - 0.004) + 0.4 * 99.7);
}

TEST(Router, TiesGoToEarlierRegisteredVenue) {
    const std::vector<VenueLiquidity> venues{{0, 0.0, {{100.0, 1.0}}}, {1, 0.0, {{100.0, 1.0}}}};
    const auto result = route(Side::Buy, 1.0, venues);
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_EQ(result.legs[0].venue, 0u);
}

TEST(Router, PartialFillWhenLiquidityRunsOut) {
    const auto result = route(Side::Buy, 3.0, {{0, 0.0, {{100.0, 1.0}}}, {1, 0.0, {{101.0, 1.0}}}});
    EXPECT_DOUBLE_EQ(result.filled_qty, 2.0);
    EXPECT_DOUBLE_EQ(route(Side::Buy, 1.0, {}).filled_qty, 0.0);
}

TEST(Router, FeeFreeSingleVenueMatchesFillSimulator) {
    const std::vector<Level> asks{{101.0, 1.0}, {102.0, 3.0}};
    const auto routed = route(Side::Buy, 2.0, {{0, 0.0, asks}});
    const auto simulated = simulate_market_fill(asks, 2.0);
    EXPECT_DOUBLE_EQ(routed.filled_qty, simulated.filled_qty);
    EXPECT_DOUBLE_EQ(routed.gross_notional / routed.filled_qty, simulated.avg_price);
}

TEST(Router, RoutedAllInNeverWorseThanAnySingleVenue) {
    std::mt19937 rng(7);
    std::uniform_real_distribution<double> price(99.0, 101.0);
    std::uniform_real_distribution<double> size(0.1, 2.0);
    std::uniform_real_distribution<double> fee(0.0, 0.01);
    for (int trial = 0; trial < 200; ++trial) {
        std::vector<VenueLiquidity> venues;
        for (std::size_t v = 0; v < 2; ++v) {
            std::vector<double> prices(5);
            for (auto& p : prices) p = price(rng);
            std::sort(prices.begin(), prices.end());
            std::vector<Level> levels;
            for (const double p : prices) levels.push_back({p, size(rng)});
            venues.push_back({v, fee(rng), levels});
        }
        const double qty = 1.0;
        const auto routed = route(Side::Buy, qty, venues);
        for (const auto& single : venues) {
            const auto alone = route(Side::Buy, qty, {single});
            if (alone.filled_qty < qty) continue;
            EXPECT_LE(all_in_notional(Side::Buy, routed), all_in_notional(Side::Buy, alone) + 1e-9);
        }
    }
}
```
(Add `#include <algorithm>`.)

- [ ] **Step 2: Build and confirm the failure** (missing header).

- [ ] **Step 3: Implement `engine/src/router.h`**
```cpp
#pragma once

#include <cstddef>
#include <vector>

#include "types.h"

namespace slipstream {

struct VenueLiquidity {
    std::size_t venue;
    double fee_rate;
    std::vector<Level> levels;  // best-first, gross prices
};

struct RouteLeg {
    std::size_t venue;
    double qty;
    double gross_notional;
    double fee;
};

struct RouteResult {
    std::vector<RouteLeg> legs;  // one per venue used, ordered by venue index
    double filled_qty = 0.0;
    double gross_notional = 0.0;
    double fees = 0.0;
};

// Takes the best fee-adjusted prices across venues until qty is filled (paper: books unchanged).
// Ties go to the venue listed first.
RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues);

// Buy: gross + fees paid. Sell: gross - fees paid.
double all_in_notional(Side side, const RouteResult& result);

}  // namespace slipstream
```
and `engine/src/router.cpp`:
```cpp
#include "router.h"

#include <algorithm>
#include <iterator>

namespace slipstream {
namespace {

struct Candidate {
    double effective_price;
    std::size_t venue;
    double fee_rate;
    Level level;
};

}  // namespace

RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues) {
    std::vector<Candidate> candidates;
    for (const auto& venue : venues) {
        const double multiplier =
            taker_side == Side::Buy ? 1.0 + venue.fee_rate : 1.0 - venue.fee_rate;
        for (const auto& level : venue.levels) {
            candidates.push_back({level.price * multiplier, venue.venue, venue.fee_rate, level});
        }
    }
    std::stable_sort(candidates.begin(), candidates.end(),
                     [taker_side](const Candidate& a, const Candidate& b) {
                         return taker_side == Side::Buy ? a.effective_price < b.effective_price
                                                        : a.effective_price > b.effective_price;
                     });

    RouteResult result;
    double remaining = qty;
    for (const auto& candidate : candidates) {
        if (remaining <= 0.0) break;
        const double take = std::min(remaining, candidate.level.qty);
        const double notional = take * candidate.level.price;
        const double fee = notional * candidate.fee_rate;
        auto leg = std::find_if(result.legs.begin(), result.legs.end(),
                                [&](const RouteLeg& l) { return l.venue == candidate.venue; });
        if (leg == result.legs.end()) {
            result.legs.push_back({candidate.venue, 0.0, 0.0, 0.0});
            leg = std::prev(result.legs.end());
        }
        leg->qty += take;
        leg->gross_notional += notional;
        leg->fee += fee;
        result.filled_qty += take;
        result.gross_notional += notional;
        result.fees += fee;
        remaining -= take;
    }
    std::sort(result.legs.begin(), result.legs.end(),
              [](const RouteLeg& a, const RouteLeg& b) { return a.venue < b.venue; });
    return result;
}

double all_in_notional(Side side, const RouteResult& result) {
    return side == Side::Buy ? result.gross_notional + result.fees
                             : result.gross_notional - result.fees;
}

}  // namespace slipstream
```
The expected values in the tests (for example 50.15 = 0.5 × 100.3) must match exactly under `EXPECT_DOUBLE_EQ`. If floating-point rounding makes one differ by more than 4 ULP, switch that single assertion to `EXPECT_NEAR(…, 1e-12)` and report it.

- [ ] **Step 4: Build, run ctest, commit** `feat(engine): add fee-aware cross-venue router`. Then run the two reviews.

### Task E3: Multi-venue engine (books, staleness, routing, counterfactuals)

**Files:** Modify `engine/src/engine.h`, `engine/src/engine.cpp`, `engine/CMakeLists.txt` (add `src/router.cpp` if not already added in E2, and the new test). Create `engine/tests/engine_routing_test.cpp`.

- [ ] **Step 1: Write the failing test `engine/tests/engine_routing_test.cpp`**
```cpp
#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

double bps(double avg, double ref) { return (avg - ref) / ref * 1e4; }

class RoutingTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10,
                  {{"kraken", 40.0}, {"coinbase", 0.0}}, 2 * kSec};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}}, kSec));
        ASSERT_TRUE(
            engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, kSec));
    }
};

}  // namespace

TEST_F(RoutingTest, SplitsChildAcrossVenuesByAllInPrice) {
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
    EXPECT_DOUBLE_EQ(fills[0].fee, 0.2);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[1].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[1].price, 100.3);
    EXPECT_DOUBLE_EQ(fills[1].fee, 0.0);

    const auto status = engine.statuses().at(0);
    const double arrival = (99.5 + 100.0) / 2.0;
    EXPECT_DOUBLE_EQ(status.arrival_mid, arrival);
    EXPECT_DOUBLE_EQ(status.fees_paid, 0.2);
    EXPECT_NEAR(status.routed_all_in_bps, bps(100.35, arrival), 1e-9);
    ASSERT_EQ(status.venue_costs.size(), 2u);
    EXPECT_EQ(status.venue_costs[0].venue, "kraken");
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_NEAR(status.venue_costs[0].all_in_bps, bps(100.4, arrival), 1e-9);
    EXPECT_EQ(status.venue_costs[1].venue, "coinbase");
    EXPECT_TRUE(status.venue_costs[1].available);
    EXPECT_NEAR(status.venue_costs[1].all_in_bps, bps(100.45, arrival), 1e-9);
}

TEST_F(RoutingTest, VenueTooThinForTheWholeChildIsUnavailable) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}}, kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    ASSERT_EQ(engine.step(kSec).size(), 2u);
    const auto status = engine.statuses().at(0);
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_FALSE(status.venue_costs[1].available);
}

TEST_F(RoutingTest, StaleVenueIsExcludedFromRoutingAndMid) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, 4 * kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 4 * kSec, kSec, 1}).accepted);
    EXPECT_DOUBLE_EQ(engine.statuses().at(0).arrival_mid, (99.5 + 100.3) / 2.0);
    const auto fills = engine.step(4 * kSec);
    ASSERT_EQ(fills.size(), 1u);  // one leg per venue; both coinbase levels merge into one leg
    EXPECT_EQ(fills[0].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[0].qty, 1.0);
    EXPECT_DOUBLE_EQ(fills[0].price, (0.5 * 100.3 + 0.5 * 100.6) / 1.0);
    EXPECT_FALSE(engine.statuses().at(0).venue_costs[0].available);
}

TEST_F(RoutingTest, CrossedConsolidatedBookSkipsTheStep) {
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    ASSERT_TRUE(engine.apply_book_update(0, {{100.5, 1.0}}, {}, kSec));
    EXPECT_TRUE(engine.step(kSec).empty());
    ASSERT_TRUE(engine.apply_book_update(0, {{100.5, 0.0}}, {}, kSec));
    EXPECT_EQ(engine.step(kSec).size(), 2u);
}

TEST_F(RoutingTest, RejectsUnknownVenueIndexAndNegativeRecvTime) {
    EXPECT_FALSE(engine.apply_book_snapshot(2, {{99.0, 1.0}}, {{100.0, 1.0}}, kSec));
    EXPECT_FALSE(engine.apply_book_snapshot(0, {{99.0, 1.0}}, {{100.0, 1.0}}, -1));
    EXPECT_EQ(engine.venue_index("coinbase"), std::optional<std::size_t>{1});
    EXPECT_FALSE(engine.venue_index("binance").has_value());
    EXPECT_EQ(engine.venue_count(), 2u);
}

TEST(RoutingSingleVenue, LegacyEngineIsUnchangedAndNeverStale) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 1000 * kSec, kSec, 1}).accepted);
    const auto fills = engine.step(1000 * kSec);
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].fee, 0.0);
    const auto status = engine.statuses().at(0);
    EXPECT_DOUBLE_EQ(status.routed_all_in_bps, status.slippage_bps);
}
```

- [ ] **Step 2: Build and confirm the failure** (the new constructor and members don't exist).

- [ ] **Step 3: Implement.** Keep all existing tests compiling unchanged.
  - **`engine.h`:**
    - Include `router.h` and `<string_view>`.
    - Add `struct VenueSettings { std::string name; double fee_bps; };` and `struct VenueCost { std::string venue; double all_in_bps; bool available; };`.
    - `Fill`: append `std::string venue; double fee;`.
    - `OrderStatus`: append `double fees_paid; double fees_bps; double routed_all_in_bps; std::vector<VenueCost> venue_costs;`.
    - Constructors: keep `Engine(RiskLimits, std::size_t book_depth)`, which delegates to the new one with `{{"kraken", 0.0}}` and `stale_ns = 0`. Add `Engine(RiskLimits, std::size_t book_depth, std::vector<VenueSettings> venues, std::int64_t stale_ns)`.
    - Add the public methods `std::optional<std::size_t> venue_index(std::string_view) const;` and `std::size_t venue_count() const;`.
    - Add venue-aware overloads `apply_book_snapshot(std::size_t venue, bids, asks, std::int64_t recv_ns)` and `apply_book_update(...)`. The existing two-argument overloads call these with venue 0 and `recv_ns = 0`.
    - Private `struct Venue { std::string name; double fee_rate; OrderBook book; std::int64_t last_update_ns; };` and `std::vector<Venue> venues_; std::int64_t stale_ns_;`. Remove the single `book_` member.
    - `ParentOrder`: append `double fees = 0.0; std::vector<double> venue_all_in_notional; std::vector<char> venue_available;`. Size both vectors to `venues_.size()` at submit, with `available = 1`.
    - Private helpers `bool fresh_locked(std::size_t venue, std::int64_t now_ns) const;`, `std::optional<double> consolidated_mid_locked(std::int64_t now_ns) const;` and `std::vector<VenueLiquidity> liquidity_locked(Side side, std::int64_t now_ns, std::optional<std::size_t> only) const;`.
  - **`engine.cpp`:**
    - **`fresh_locked`:** `venues_.size() < 2 || now_ns - venues_[v].last_update_ns <= stale_ns_`. Both operands are ≥ 0, because recv_ns is validated and now_ns comes from callers; guard with `now_ns < last_update_ns → fresh`.
    - **`consolidated_mid_locked(now)`:** take the maximum best bid and the minimum best ask over the fresh venues. Return `nullopt` if either is missing, or if `bid >= ask` (crossed). Otherwise return `(bid + ask) / 2`.
    - **`mid()`** (public, used by GetStatus) returns `consolidated_mid_locked(latest recv_ns seen)`. Keep a member `std::int64_t latest_ns_ = 0`, updated on every apply and step.
    - **`submit`:** uses `consolidated_mid_locked(request.start_ns)` as the arrival mid, and routes the immediate-cost benchmark with `route(side, qty, liquidity_locked(side, start_ns, nullopt))`. `immediate_cost_bps` is gross, from `gross_notional / filled_qty`, which is unchanged for a single venue.
    - **`step`:** compute `ref = consolidated_mid_locked(now_ns)` once. In `advance_locked`, replace `simulate_market_fill` with `route(side, child, liquidity_locked(side, now_ns, nullopt))`, then:
      - Push one `Fill{order_id, now_ns, leg.qty, leg.gross_notional / leg.qty, venues_[leg.venue].name, leg.fee}` per leg.
      - Add `filled_qty`, `gross_notional` and `fees` to the order, and update the position by `routed.filled_qty`.
      - **Counterfactuals:** for each venue `v`, if it is not fresh, set `available[v] = 0`. Otherwise compute `alone = route(side, routed.filled_qty, liquidity_locked(side, now_ns, v))`. If `alone.filled_qty < routed.filled_qty * (1 - 1e-9)`, set `available[v] = 0`; else add `all_in_notional(side, alone)` to `venue_all_in_notional[v]`.
    - **`statuses()`:**
      - `fees_paid = order.fees`.
      - `fees_bps = filled_notional > 0 ? fees / filled_notional * 1e4 : 0`.
      - `all_in_avg = filled_qty > 0 ? (buy ? notional + fees : notional - fees) / filled_qty : 0`.
      - `routed_all_in_bps = cost_bps(side, all_in_avg, arrival_mid)`.
      - For each venue, `VenueCost{name, available && filled_qty > 0 ? cost_bps(side, venue_all_in_notional[v] / filled_qty, arrival) : 0.0, available[v] && filled_qty > 0}`.
    - **`apply_book_*`:** reject `venue >= venues_.size()` and `recv_ns < 0`. On success set `last_update_ns = max(last_update_ns, recv_ns)`.
    - **Constructor:** converts `fee_bps` to `fee_rate = fee_bps / 1e4`. Each `Venue` gets its own `OrderBook(book_depth)`.

- [ ] **Step 4: Build.** All existing tests plus the new ones must pass. The single-venue legacy tests prove backward compatibility. Commit `feat(engine): route children across venue books with staleness guard and counterfactuals`. Then run the two reviews.

### Task E4: Proto + service + main

**Files:** Modify `proto/slipstream/v1/execution.proto`, `engine/src/service.{h,cpp}`, `engine/src/main.cpp`, `engine/tests/service_test.cpp`.

- [ ] **Step 1: Proto (additive)**
  - `BookUpdate`: add `string venue = 5; int64 recv_ns = 6;`.
  - `TradeBatch`: add `string venue = 3;`.
  - `Fill`: add `string venue = 5; double fee = 6;`.
  - Add `message VenueCost { string venue = 1; double all_in_bps = 2; bool available = 3; }`.
  - `OrderStatus`: add `double fees_paid = 12; double fees_bps = 13; double routed_all_in_bps = 14; repeated VenueCost venue_costs = 15;`.

- [ ] **Step 2: Failing tests** (append to `service_test.cpp`; the existing `ServiceTest` uses the legacy single-venue engine):
```cpp
TEST_F(ServiceTest, EmptyVenueMeansTheOnlyVenue) {
    const auto update = snapshot();  // venue unset
    v1::BookAck ack;
    EXPECT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());
}

TEST(ServiceMultiVenue, VenueNamesAreValidated) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 0.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    v1::BookUpdate update;
    update.set_symbol("BTC/USD");
    update.set_is_snapshot(true);
    auto* bid = update.add_bids();
    bid->set_price(99.0);
    bid->set_qty(1.0);
    auto* ask = update.add_asks();
    ask->set_price(100.0);
    ask->set_qty(1.0);
    v1::BookAck ack;
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);  // empty venue with two registered
    update.set_venue("binance");
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    update.set_venue("coinbase");
    update.set_recv_ns(-5);
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    update.set_recv_ns(5);
    EXPECT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());

    v1::TradeBatch trades;
    trades.set_symbol("BTC/USD");
    trades.set_venue("binance");
    auto* trade = trades.add_trades();
    trade->set_price(100.0);
    trade->set_qty(1.0);
    v1::TradeAck trade_ack;
    EXPECT_EQ(service.ApplyTrades(nullptr, &trades, &trade_ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    trades.set_venue("kraken");
    EXPECT_TRUE(service.ApplyTrades(nullptr, &trades, &trade_ack).ok());
}

TEST(ServiceMultiVenue, StepAndStatusCarryVenueFields) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 0.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}}, 0));
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, 0));
    v1::ParentOrder order;
    order.set_order_id("o-1");
    order.set_side(v1::SIDE_BUY);
    order.set_qty(1.0);
    order.set_duration_ns(1'000'000'000);
    order.set_num_slices(1);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &order, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::StepRequest step;
    v1::StepReply step_reply;
    ASSERT_TRUE(service.Step(nullptr, &step, &step_reply).ok());
    ASSERT_EQ(step_reply.fills_size(), 2);
    EXPECT_EQ(step_reply.fills(0).venue(), "kraken");
    EXPECT_DOUBLE_EQ(step_reply.fills(0).fee(), 0.2);
    v1::StatusRequest status_request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &status_request, &status).ok());
    EXPECT_DOUBLE_EQ(status.orders(0).fees_paid(), 0.2);
    ASSERT_EQ(status.orders(0).venue_costs_size(), 2);
    EXPECT_EQ(status.orders(0).venue_costs(1).venue(), "coinbase");
    EXPECT_TRUE(status.orders(0).venue_costs(1).available());
}
```

- [ ] **Step 3: Implement the service**
  - Add a private helper `std::optional<std::size_t> resolve_venue(const std::string& name) const`. For an empty name, return `0` if `engine_.venue_count() == 1`, otherwise `nullopt`. For any other name, return `engine_.venue_index(name)`.
  - **`ApplyBookUpdate`:** after the symbol and level checks, validate the venue (`invalid("unknown venue")`) and `recv_ns >= 0` (`invalid("invalid recv_ns")`). Then call the venue-aware engine methods.
  - **`ApplyTrades`:** validate the venue the same way. Volume stays consolidated.
  - **`Step`:** set `venue` and `fee` on each fill.
  - **`GetStatus`:** set `fees_paid`, `fees_bps` and `routed_all_in_bps`, and add the `venue_costs`.
  - **`main.cpp`:** build `std::vector<VenueSettings>` from `config.venues`, and construct `Engine(config.limits, config.book_depth, venues, config.stale_ns)`. Add the venues to the startup line, e.g. `(paper mode, symbol BTC/USD, venues kraken:40bps coinbase:0bps)`. The Python fixture only parses `port (\d+)`, so this stays compatible.

- [ ] **Step 4: Full gate:** `bash scripts/ci.sh` → `CI OK` (Python regenerates the stubs; the proto change is additive). Commit `feat(engine): expose venues, fees, and routing results over gRPC`. Run the two reviews, then finish Track E: push, open PR 1, merge when green.

---

# Track P — Python venues (PR 2)

### Task P1: Coinbase parser

**Files:** Create `python/slipstream/coinbase.py`, `python/tests/test_coinbase.py`; modify `python/slipstream/models.py` (add venue).

- [ ] **Step 1: Models.** In `models.py`:
  - Add `Venue = Literal["kraken", "coinbase"]`.
  - Append `venue: Venue = "kraken"` as the last field of `BookUpdate` and `TradeBatch`. The default keeps every existing construction and comparison valid.

- [ ] **Step 2: Failing tests `python/tests/test_coinbase.py`**
```python
import json
from typing import Any

import pytest

from slipstream.coinbase import (
    COINBASE_WS_URL,
    CoinbaseMessageError,
    CoinbaseStream,
    subscribe_messages,
)
from slipstream.models import BookUpdate, TradeBatch


def l2(seq: int, kind: str, updates: list[dict[str, str]], product: str = "BTC-USD") -> str:
    return json.dumps({
        "channel": "l2_data", "client_id": "", "timestamp": "2026-09-24T00:00:00Z",
        "sequence_num": seq,
        "events": [{"type": kind, "product_id": product, "updates": updates}],
    })


def upd(side: str, price: str, qty: str) -> dict[str, str]:
    return {"side": side, "event_time": "2026-09-24T00:00:00Z", "price_level": price,
            "new_quantity": qty}


def trades(seq: int, kind: str, rows: list[dict[str, str]]) -> str:
    return json.dumps({"channel": "market_trades", "client_id": "",
                       "timestamp": "2026-09-24T00:00:00Z", "sequence_num": seq,
                       "events": [{"type": kind, "trades": rows}]})


def trade(price: str, size: str, product: str = "BTC-USD") -> dict[str, str]:
    return {"trade_id": "1", "product_id": product, "price": price, "size": size,
            "side": "BUY", "time": "2026-09-24T00:00:00Z"}


def test_endpoint_and_subscriptions() -> None:
    assert COINBASE_WS_URL == "wss://advanced-trade-ws.coinbase.com"
    assert [json.loads(m) for m in subscribe_messages("BTC/USD")] == [
        {"type": "subscribe", "channel": "level2", "product_ids": ["BTC-USD"]},
        {"type": "subscribe", "channel": "market_trades", "product_ids": ["BTC-USD"]},
        {"type": "subscribe", "channel": "heartbeats", "product_ids": ["BTC-USD"]},
    ]


def test_snapshot_is_sorted_truncated_and_tagged() -> None:
    stream = CoinbaseStream("BTC/USD", depth=2)
    raw = l2(0, "snapshot", [upd("bid", "99", "1"), upd("bid", "98", "2"), upd("bid", "99.5", "3"),
                             upd("offer", "101", "1"), upd("offer", "100.5", "2"),
                             upd("offer", "102", "0")])
    assert stream.parse(raw) == BookUpdate(
        "BTC/USD", True, ((99.5, 3.0), (99.0, 1.0)), ((100.5, 2.0), (101.0, 1.0)), "coinbase")


def test_update_passes_deltas_through() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(0, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    assert stream.parse(l2(1, "update", [upd("bid", "99", "0"), upd("offer", "100.9", "4")])) == (
        BookUpdate("BTC/USD", False, ((99.0, 0.0),), ((100.9, 4.0),), "coinbase"))


def test_trades_update_and_snapshot() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    assert stream.parse(trades(0, "snapshot", [trade("100", "1")])) is None
    assert stream.parse(trades(1, "update", [trade("100.5", "0.2"), trade("100.4", "0.3")])) == (
        TradeBatch("BTC/USD", False, ((100.5, 0.2), (100.4, 0.3)), "coinbase"))


def test_heartbeats_and_subscriptions_are_ignored() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    heartbeat = {"channel": "heartbeats", "client_id": "", "timestamp": "t", "sequence_num": 0,
                 "events": [{"current_time": "t", "heartbeat_counter": 1}]}
    subs = {"channel": "subscriptions", "client_id": "", "timestamp": "t", "sequence_num": 1,
            "events": [{"subscriptions": {"level2": ["BTC-USD"]}}]}
    assert stream.parse(json.dumps(heartbeat)) is None
    assert stream.parse(json.dumps(subs)) is None


def test_sequence_gap_raises() -> None:
    stream = CoinbaseStream("BTC/USD", depth=10)
    stream.parse(l2(5, "snapshot", [upd("bid", "99", "1"), upd("offer", "101", "1")]))
    with pytest.raises(CoinbaseMessageError, match="sequence gap"):
        stream.parse(l2(7, "update", [upd("bid", "99", "2")]))


def test_error_message_raises() -> None:
    with pytest.raises(CoinbaseMessageError, match="coinbase error"):
        CoinbaseStream("BTC/USD", 10).parse(json.dumps({"type": "error", "message": "bad"}))


@pytest.mark.parametrize(
    "raw",
    [
        "not json", "[" * 100_000, "1" * 5000, json.dumps([1]),
        l2(0, "snapshot", [upd("bid", "99", "1")], product="ETH-USD"),
        l2(0, "weird", []),
        l2(0, "update", [upd("middle", "99", "1")]),
        l2(0, "update", [upd("bid", "-1", "1")]),
        l2(0, "update", [upd("bid", "99", "-1")]),
        l2(0, "update", [upd("bid", "nan", "1")]),
        l2(0, "update", [upd("bid", "1e400", "1")]),
        l2(0, "update", [upd("bid", "1" * 65, "1")]),
        l2(0, "update", [{"side": "bid", "price_level": 99, "new_quantity": "1"}]),
        json.dumps({"channel": "l2_data", "sequence_num": True, "events": []}),
        json.dumps({"channel": "l2_data", "sequence_num": 0, "events": "x"}),
        trades(0, "update", [trade("100", "0")]),
        trades(0, "update", [trade("100", "1", product="ETH-USD")]),
    ],
    ids=["not-json", "deep", "huge-int", "not-object", "wrong-product", "bad-type", "bad-side",
         "neg-price", "neg-qty", "nan", "overflow", "long", "number-not-string", "bool-seq",
         "events-not-list", "zero-trade", "trade-wrong-product"],
)
def test_hostile_input_only_raises_coinbase_error(raw: str) -> None:
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("BTC/USD", depth=10).parse(raw)


def test_rejects_unknown_symbol_and_bad_depth() -> None:
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("DOGE/XYZ", depth=10)
    with pytest.raises(CoinbaseMessageError):
        CoinbaseStream("BTC/USD", depth=0)
```

- [ ] **Step 3: Implement `python/slipstream/coinbase.py`**
```python
from __future__ import annotations

import json
import math
from typing import Any

from slipstream.models import BookUpdate, TradeBatch

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
MAX_MESSAGE_BYTES = 16 << 20
MAX_EVENTS = 16
MAX_UPDATES = 200_000
MAX_TRADES = 1000
MAX_DEPTH = 1000
MAX_FIELD_CHARS = 64
_PRODUCTS = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}
_SIDES = {"bid", "offer"}


class CoinbaseMessageError(ValueError):
    pass


def product_id(symbol: str) -> str:
    try:
        return _PRODUCTS[symbol]
    except KeyError as exc:
        raise CoinbaseMessageError(f"no Coinbase product for {symbol!r}") from exc


def subscribe_messages(symbol: str) -> list[str]:
    product = product_id(symbol)
    return [
        json.dumps({"type": "subscribe", "channel": channel, "product_ids": [product]})
        for channel in ("level2", "market_trades", "heartbeats")
    ]


class CoinbaseStream:
    """Parses one Coinbase WebSocket connection; sequence numbers are per connection."""

    def __init__(self, symbol: str, depth: int) -> None:
        if not 1 <= depth <= MAX_DEPTH:
            raise CoinbaseMessageError("depth must be between 1 and 1000")
        self._symbol = symbol
        self._product = product_id(symbol)
        self._depth = depth
        self._last_sequence: int | None = None

    def parse(self, raw: str | bytes) -> BookUpdate | TradeBatch | None:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise CoinbaseMessageError("message too large")
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise CoinbaseMessageError("invalid JSON") from exc
        if not isinstance(msg, dict):
            raise CoinbaseMessageError("message is not an object")
        if msg.get("type") == "error":
            raise CoinbaseMessageError(f"coinbase error: {str(msg.get('message'))[:200]!r}")
        self._check_sequence(msg.get("sequence_num"))
        channel = msg.get("channel")
        if channel == "l2_data":
            return self._book(_events(msg))
        if channel == "market_trades":
            return self._trades(_events(msg))
        return None

    def _check_sequence(self, sequence: Any) -> None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CoinbaseMessageError("invalid sequence_num")
        if self._last_sequence is not None and sequence != self._last_sequence + 1:
            raise CoinbaseMessageError("sequence gap")
        self._last_sequence = sequence

    def _book(self, events: list[dict[str, Any]]) -> BookUpdate:
        kinds = {event.get("type") for event in events}
        if kinds - {"snapshot", "update"} or len(kinds) != 1:
            raise CoinbaseMessageError(f"unexpected level2 event types: {sorted(map(str, kinds))}")
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for event in events:
            if event.get("product_id") != self._product:
                raise CoinbaseMessageError("unexpected product")
            updates = event.get("updates")
            if not isinstance(updates, list) or len(updates) > MAX_UPDATES:
                raise CoinbaseMessageError("updates must be a bounded list")
            for update in updates:
                if not isinstance(update, dict) or update.get("side") not in _SIDES:
                    raise CoinbaseMessageError("invalid level2 update")
                price = _decimal(update.get("price_level"), "price", allow_zero=False)
                qty = _decimal(update.get("new_quantity"), "quantity", allow_zero=True)
                (bids if update["side"] == "bid" else asks).append((price, qty))
        is_snapshot = kinds == {"snapshot"}
        if is_snapshot:
            bids = sorted((level for level in bids if level[1] > 0), reverse=True)[: self._depth]
            asks = sorted(level for level in asks if level[1] > 0)[: self._depth]
        return BookUpdate(self._symbol, is_snapshot, tuple(bids), tuple(asks), "coinbase")

    def _trades(self, events: list[dict[str, Any]]) -> TradeBatch | None:
        prints: list[tuple[float, float]] = []
        for event in events:
            kind = event.get("type")
            if kind not in ("snapshot", "update"):
                raise CoinbaseMessageError(f"unexpected trade event type: {kind!r}")
            rows = event.get("trades")
            if not isinstance(rows, list) or len(rows) > MAX_TRADES:
                raise CoinbaseMessageError("trades must be a bounded list")
            if kind == "snapshot":
                continue
            for row in rows:
                if not isinstance(row, dict) or row.get("product_id") != self._product:
                    raise CoinbaseMessageError("invalid trade")
                price = _decimal(row.get("price"), "trade price", allow_zero=False)
                size = _decimal(row.get("size"), "trade size", allow_zero=False)
                prints.append((price, size))
        if not prints:
            return None
        return TradeBatch(self._symbol, False, tuple(prints), "coinbase")


def _events(msg: dict[str, Any]) -> list[dict[str, Any]]:
    events = msg.get("events")
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise CoinbaseMessageError("events must be a non-empty bounded list")
    if not all(isinstance(event, dict) for event in events):
        raise CoinbaseMessageError("event must be an object")
    return events


def _decimal(value: Any, name: str, *, allow_zero: bool) -> float:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise CoinbaseMessageError(f"{name} must be a short decimal string")
    try:
        result = float(value)
    except ValueError as exc:
        raise CoinbaseMessageError(f"{name} is not a number") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise CoinbaseMessageError(f"{name} out of range")
    return result
```
The `events-not-list` case (`"events": "x"`) and an empty events list both reach `_events` and raise. Heartbeats and subscriptions are only read for their sequence number, so their event contents are ignored.

- [ ] **Step 5: Run the tests (pass), then the gate. Commit** `feat(orchestrator): add strict Coinbase Advanced Trade market data parser`. Then run the two reviews.

### Task P2: Venue-aware runner

**Files:** Modify `python/slipstream/runner.py`, `python/tests/test_runner.py`, `python/tests/conftest.py` (if needed).

- [ ] **Step 1: Failing tests** (append):
```python
def cb_snapshot(seq: int = 0) -> str:
    return json.dumps({"channel": "l2_data", "sequence_num": seq, "events": [
        {"type": "snapshot", "product_id": "BTC-USD",
         "updates": [{"side": "bid", "price_level": "99", "new_quantity": "1"},
                     {"side": "offer", "price_level": "101", "new_quantity": "1"}]}]})


def test_waits_for_a_snapshot_from_every_venue(fake_engine: FakeEngine) -> None:
    runner = ExecutionRunner(fake_engine, SPEC, "BTC/USD", logging.getLogger("t"),
                             venues=("kraken", "coinbase"))
    runner.on_message(snapshot(), 100, "kraken")
    assert fake_engine.submits == []
    runner.on_message(cb_snapshot(), 200, "coinbase")
    assert [s.order_id for s, _, _ in fake_engine.submits] == ["o-1"]
    assert [book.venue for book in fake_engine.books] == ["kraken", "coinbase"]


def test_messages_from_unconfigured_venue_are_rejected(fake_engine: FakeEngine) -> None:
    with pytest.raises(ValueError, match="venue"):
        make_runner(fake_engine).on_message(cb_snapshot(), 1, "coinbase")
```

- [ ] **Step 2: Implement.** `ExecutionRunner.__init__` gains `venues: Sequence[Venue] = ("kraken",)` and `book_depth: int = 10`.
  - Build parsers `{"kraken": parse_message, "coinbase": CoinbaseStream(symbol, book_depth).parse}`, restricted to the configured venues.
  - `on_message(raw, now_ns, venue: Venue = "kraken")`:
    - An unknown or unconfigured venue raises `ValueError(f"unconfigured venue {venue!r}")`.
    - Parse with that venue's parser.
    - Book updates: record the venue in `self._snapshots` when the update is a snapshot. Submit once `self._snapshots` covers every configured venue, calibrating on the snapshot that completes the set. (Consolidated-book calibration is PR 3 scope; PR 2 keeps calibrating on the triggering snapshot.)
  - The CLI still only uses Kraken until PR 3.
  - `ExecutionRunner` must still accept its current positional arguments.

- [ ] **Step 3: Run the tests and the gate. Commit** `feat(orchestrator): dispatch messages to per-venue parsers in the runner`. Then run the two reviews.

### Task P3: Concurrent feeds in `live`

**Files:** Modify `python/slipstream/live.py`, `python/tests/test_live.py`.

- [ ] **Step 1: Failing tests.**
  - Change the existing tests' `run_live(..., url=url)` calls to `urls={"kraken": url}`.
  - Add a two-server test: Kraken sends its ack and a snapshot, and Coinbase sends three subscription acks plus an `l2_data` snapshot. The runner is configured with `venues=("kraken", "coinbase")` and `done_after_steps=1`. Assert the fake engine received a submit, and that the Coinbase server received three subscriptions (`level2`, `market_trades`, `heartbeats`).
  - Add a test where the Coinbase server closes immediately after accepting. `run_live` must raise `LiveFeedError`.

- [ ] **Step 2: Implement**
```python
async def run_live(
    runner: ExecutionRunner,
    symbol: str,
    depth: int,
    deadline_s: float,
    idle_timeout_s: float = 30.0,
    venues: Sequence[Venue] = ("kraken",),
    urls: Mapping[Venue, str] | None = None,
) -> None:
    endpoints: dict[Venue, str] = {"kraken": KRAKEN_WS_URL, "coinbase": COINBASE_WS_URL}
    endpoints.update(urls or {})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    finished = asyncio.Event()

    async def feed(venue: Venue) -> None:
        max_size = COINBASE_MAX_BYTES if venue == "coinbase" else MAX_MESSAGE_BYTES
        async with connect(endpoints[venue], max_size=max_size, open_timeout=10) as ws:
            for message in _subscriptions(venue, symbol, depth):
                await ws.send(message)
            while not finished.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise LiveFeedError("order did not finish before the deadline")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(idle_timeout_s, remaining))
                except TimeoutError as exc:
                    if loop.time() >= deadline:
                        raise LiveFeedError("order did not finish before the deadline") from exc
                    raise LiveFeedError(f"no market data from {venue} (idle timeout)") from exc
                runner.on_message(raw, time.time_ns(), venue)
                if runner.is_done():
                    finished.set()

    try:
        async with asyncio.TaskGroup() as group:
            for venue in venues:
                group.create_task(feed(venue))
    except ExceptionGroup as errors:
        first = errors.exceptions[0]
        if isinstance(first, (WebSocketException, OSError)):
            raise LiveFeedError(f"market data connection failed: {first}") from first
        raise first from None
```
  - `_subscriptions(venue, symbol, depth)` returns `[subscribe_message(symbol, depth), subscribe_trades_message(symbol)]` for Kraken and `coinbase.subscribe_messages(symbol)` for Coinbase.
  - Import `MAX_MESSAGE_BYTES as COINBASE_MAX_BYTES` from `coinbase`.
  - When one feed finishes, the other stops at its next message; heartbeats make that at most about 1 s.

- [ ] **Step 3: Run the tests and the gate. Commit** `feat(orchestrator): stream several venues concurrently in live mode`. Then run the two reviews.

### Task P4: Multi-venue record and replay

**Files:** Modify `python/slipstream/recorder.py`, `python/slipstream/replay.py`, `python/tests/test_recorder.py`, `python/tests/test_replay.py`, and callers in `cli.py` / tests that unpack `read_replay` tuples.

- [ ] **Step 1: Failing tests.**
  - `read_replay` now yields `(recv_ns, raw, venue)`:
    - A line without `"venue"` yields `"kraken"`.
    - `"venue": "binance"` or a non-string venue raises `ReplayError("line N: ...")`.
  - `run_replay` passes the venue to `runner.on_message`.
  - Update every existing test that unpacks two-tuples.
  - `record_stream(handle, symbol, depth, duration_s, venues=("kraken","coinbase"), urls=..., clock=...)`:
    - It writes lines with `"venue"`.
    - A round-trip test with two local servers checks that the replayed venues and order match.
    - A hostile Coinbase message aborts the recording with `CoinbaseMessageError`.

- [ ] **Step 2: Implement.**
  - **`record_stream`** follows the same TaskGroup shape as `run_live`, with an ExceptionGroup unwrap into `RecordError` for network errors. Each venue task has its own validator: `parse_message` for Kraken and a per-task `CoinbaseStream(symbol, depth).parse` for Coinbase. Validate each message before writing it. Every write goes through a single function, so lines never interleave.
  - **`read_replay`:**
    - Accept an optional `"venue"` in `{"kraken", "coinbase"}`; default to `"kraken"`.
    - Yield three-tuples.
  - **`run_replay`:** take a `(recv_ns, raw, venue)` iterable and pass the venue along.
  - **CLI:** keep `record` defaulting to Kraken only; `--venues` arrives in PR 3.

- [ ] **Step 3: Full gate in WSL** (`bash scripts/ci.sh` → `CI OK`, including both existing end-to-end tests). Commit `feat(orchestrator): record and replay multi-venue sessions`. Run the two reviews, then finish Track P: push, open PR 2, merge when green.

---

## After PRs 1 and 2 merge
Write `docs/superpowers/plans/<date>-smart-routing-integration.md` (PR 3), covering:
- **Engine client:** `apply_book(update, recv_ns)` sends `venue` and `recv_ns`; `apply_trades` sends `venue`; `Fill` gains `venue` and `fee`.
- **Runner:** passes `now_ns` as `recv_ns`, and calibrates η on the consolidated fee-adjusted book. That needs the fees in Python, so they come from a `--fee-bps kraken=40,coinbase=60` CLI option that must match the engine's `--venue` flags. The demo scripts pass both from one variable.
- **CLI:** `--venues` on `live`/`replay`/`compare`/`record`.
- **Reports:** the summary and comparison gain fees bps, routed all-in, per-venue all-in or `n/a`, and saved vs best single venue.
- **Tests and demos:** a two-venue end-to-end fixture with hand-derived legs; a live two-venue `compare` run; README and note.md updates.
