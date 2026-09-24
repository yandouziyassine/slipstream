# Execution Schedules — PR 1 (Engine) + PR 2 (Python Data) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the VWAP, POV and Almgren-Chriss schedules to the C++ engine behind a `Schedule` interface, with an `ApplyTrades` RPC. On the Python side, add Kraken OHLC fetching, calibration (VWAP weights, σ, η), trade-channel parsing, and a `record` command whose files replay with their calibration data included.

**Architecture:** The engine keeps its deterministic step loop and risk gate, and only the schedule becomes polymorphic (`std::unique_ptr<Schedule>`, built from a `std::variant` spec). Python fetches and validates all market data; the engine never touches the network. PR 3 (a separate plan, written after PRs 1 and 2 merge) wires `--algo`, `compare`, and the four-algorithm end-to-end test.

**Tech Stack:** C++20, gRPC 1.51, GoogleTest, ASan + UBSan · Python 3.12, stdlib `urllib` / `statistics`, websockets, pytest, mypy --strict, ruff.

**Spec:** `docs/superpowers/specs/2026-09-24-execution-schedules-design.md`

---

## Conventions for every task

- **Environment:** WSL Ubuntu 24.04 (toolchain installed; Python venv at `~/.venvs/slipstream`) or the Docker dev container, which now works. From Git Bash, run WSL commands through a script file under the session scratchpad, e.g.
  `MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash /mnt/c/.../scratchpad/run.sh 2>&1 | tr -d '\0'`,
  where the script does `export PATH="$HOME/.venvs/slipstream/bin:$PATH"; cd /mnt/c/Users/yando/OneDrive/Documents/Code/Project1/.worktrees/<name>; …`.
- **Build and test:** `bash scripts/gen_proto.sh && bash scripts/build_engine.sh && ctest --test-dir build/engine --output-on-failure`, and from `python/`: `pytest -q && mypy slipstream && ruff check . && ruff format --check .`. Final gate: `bash scripts/ci.sh` must print `CI OK`.
- **Commits:** `GIT_AUTHOR_EMAIL=160782497+yandouziyassine@users.noreply.github.com GIT_COMMITTER_EMAIL=160782497+yandouziyassine@users.noreply.github.com git commit …`, with the message in a HEREDOC ending with the `Co-Authored-By` trailer for the model used. Never change git config. Never push. Use LF line endings.
- **C++ warnings:** `-Wall -Wextra -Wpedantic -Wshadow -Werror`. A range-for over an initializer list of `std::string` must use `const auto&`.

## Parallel tracks

| Track | Branch / worktree | Tasks |
|---|---|---|
| E (engine) | `feat/schedules-engine` at `.worktrees/schedules-engine`, from `main` | E1 → E6 |
| P (Python data) | `feat/market-data` at `.worktrees/market-data`, from `main` | P1 → P4 |

The tracks touch disjoint files, except that E6 changes `execution.proto`; P never edits it.

---

# Track E — Engine (PR 1)

### Task E1: `Schedule` interface, shared slice rules, TWAP refactor

**Files:**
- Create: `engine/src/schedule.h`, `engine/src/slicing.h`, `engine/src/slicing.cpp`
- Modify: `engine/src/twap.h`, `engine/src/twap.cpp`, `engine/src/engine.cpp` (one call site), `engine/CMakeLists.txt` (add `src/slicing.cpp` to `slipstream_core`)
- Test: rewrite `engine/tests/twap_test.cpp`

- [ ] **Step 1: Rewrite the test `engine/tests/twap_test.cpp`**

```cpp
#include "twap.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <limits>

using slipstream::kMaxSlices;
using slipstream::MarketState;
using slipstream::TwapSchedule;

namespace {
const MarketState kNoMarket{0.0};
}

TEST(Twap, RejectsInvalidParams) {
    EXPECT_FALSE(TwapSchedule::create({0.0, 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({-1.0, 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({std::nan(""), 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 0, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 60, 0}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 60, kMaxSlices + 1}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 5, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, -1, 60, 6}));
}

TEST(Twap, NothingReleasedBeforeStart) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(999, kNoMarket), 0.0);
}

TEST(Twap, FirstSliceReleasedAtStart) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1000, kNoMarket), 0.2);
}

TEST(Twap, OneSliceReleasedPerInterval) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1099, kNoMarket), 0.2);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1100, kNoMarket), 0.4);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1250, kNoMarket), 0.6);
}

TEST(Twap, ReleasesExactTotalAfterLastSlice) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_EQ(schedule->target_qty_at(1500, kNoMarket), 1.2);
    EXPECT_EQ(schedule->target_qty_at(10'000'000, kNoMarket), 1.2);
}

TEST(Twap, ExtremeTimestampsDoNotOverflow) {
    const auto schedule = TwapSchedule::create({1.0, 0, 1, 1});
    ASSERT_TRUE(schedule);
    EXPECT_EQ(schedule->target_qty_at(std::numeric_limits<std::int64_t>::max(), kNoMarket), 1.0);
    const auto many = TwapSchedule::create({2.0, 0, 1000, 1000});
    ASSERT_TRUE(many);
    EXPECT_EQ(many->target_qty_at(std::numeric_limits<std::int64_t>::max(), kNoMarket), 2.0);
    EXPECT_DOUBLE_EQ(many->target_qty_at(std::numeric_limits<std::int64_t>::min(), kNoMarket), 0.0);
}

TEST(Twap, NeverExpiresAndReportsName) {
    const auto schedule = TwapSchedule::create({1.0, 0, 60, 6});
    ASSERT_TRUE(schedule);
    EXPECT_FALSE(schedule->expired(std::numeric_limits<std::int64_t>::max()));
    EXPECT_STREQ(schedule->name(), "twap");
}
```

- [ ] **Step 2: Build and confirm it fails.** Expected: compile errors (`kMaxSlices` / `MarketState` not declared, `target_qty_at` arity).

- [ ] **Step 3: Create `engine/src/schedule.h`**

```cpp
#pragma once

#include <cstdint>

namespace slipstream {

struct MarketState {
    double cumulative_volume;
};

class Schedule {
public:
    virtual ~Schedule() = default;

    // Cumulative quantity that should be complete by now_ns.
    virtual double target_qty_at(std::int64_t now_ns, const MarketState& market) const = 0;

    // True once the unfilled remainder should be halted instead of worked further.
    virtual bool expired(std::int64_t /*now_ns*/) const { return false; }

    virtual const char* name() const = 0;

protected:
    Schedule() = default;
    Schedule(const Schedule&) = default;
    Schedule& operator=(const Schedule&) = default;
};

}  // namespace slipstream
```

- [ ] **Step 4: Create `engine/src/slicing.h` and `engine/src/slicing.cpp`**

```cpp
#pragma once

#include <cstdint>

namespace slipstream {

struct SliceParams {
    double total_qty;
    std::int64_t start_ns;
    std::int64_t duration_ns;
    std::int32_t num_slices;
};

constexpr std::int32_t kMaxSlices = 1000;

bool valid_slice_params(const SliceParams& params);

// 0 before start, 1 at start, +1 per interval, capped at num_slices. Overflow-safe for any now_ns.
std::int64_t released_slices(const SliceParams& params, std::int64_t now_ns);

}  // namespace slipstream
```

```cpp
#include "slicing.h"

#include <cmath>

namespace slipstream {

bool valid_slice_params(const SliceParams& params) {
    if (!std::isfinite(params.total_qty) || params.total_qty <= 0.0) return false;
    if (params.start_ns < 0 || params.duration_ns <= 0) return false;
    if (params.num_slices < 1 || params.num_slices > kMaxSlices) return false;
    return params.duration_ns >= params.num_slices;
}

std::int64_t released_slices(const SliceParams& params, std::int64_t now_ns) {
    if (now_ns < params.start_ns) return 0;
    const std::int64_t interval_ns = params.duration_ns / params.num_slices;
    const std::int64_t intervals_elapsed = (now_ns - params.start_ns) / interval_ns;
    if (intervals_elapsed >= params.num_slices - 1) return params.num_slices;
    return intervals_elapsed + 1;
}

}  // namespace slipstream
```

- [ ] **Step 5: Replace `engine/src/twap.h` and `engine/src/twap.cpp`**

```cpp
#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class TwapSchedule final : public Schedule {
public:
    static std::optional<TwapSchedule> create(const SliceParams& params);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "twap"; }

private:
    explicit TwapSchedule(const SliceParams& params) : params_(params) {}

    SliceParams params_;
};

}  // namespace slipstream
```

```cpp
#include "twap.h"

namespace slipstream {

std::optional<TwapSchedule> TwapSchedule::create(const SliceParams& params) {
    if (!valid_slice_params(params)) return std::nullopt;
    return TwapSchedule(params);
}

double TwapSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const std::int64_t released = released_slices(params_, now_ns);
    if (released == params_.num_slices) return params_.total_qty;
    return params_.total_qty * static_cast<double>(released) /
           static_cast<double>(params_.num_slices);
}

}  // namespace slipstream
```

- [ ] **Step 6: Keep the engine compiling.** In `engine/src/engine.cpp`, change `order.schedule.target_qty_at(now_ns)` to `order.schedule.target_qty_at(now_ns, MarketState{})`. Add `src/slicing.cpp` to `slipstream_core` in `engine/CMakeLists.txt`.

- [ ] **Step 7: Build and run all ctest.** Expected: every test passes (the TWAP count grows by one). Commit `engine`: `refactor(engine): introduce Schedule interface and shared slice rules`.

### Task E2: VWAP schedule

**Files:** Create `engine/src/vwap.h`, `engine/src/vwap.cpp`, and `engine/tests/vwap_test.cpp`. Add both to CMake (the source to `slipstream_core`, the test to `engine_tests`).

- [ ] **Step 1: Write the test `engine/tests/vwap_test.cpp`**

```cpp
#include "vwap.h"

#include <gtest/gtest.h>

#include <cmath>
#include <limits>

#include "twap.h"

using namespace slipstream;

namespace {
const MarketState kNoMarket{0.0};
constexpr SliceParams kParams{1.0, 0, 400, 4};
}  // namespace

TEST(Vwap, RejectsBadWeights) {
    EXPECT_FALSE(VwapSchedule::create(kParams, {1.0, 1.0, 1.0}));
    EXPECT_FALSE(VwapSchedule::create(kParams, {1.0, 1.0, 0.0, 1.0}));
    EXPECT_FALSE(VwapSchedule::create(kParams, {1.0, -1.0, 1.0, 1.0}));
    EXPECT_FALSE(VwapSchedule::create(kParams, {1.0, std::nan(""), 1.0, 1.0}));
    EXPECT_FALSE(VwapSchedule::create(
        kParams, {1.0, std::numeric_limits<double>::infinity(), 1.0, 1.0}));
    EXPECT_FALSE(VwapSchedule::create(kParams, {1e308, 1e308, 1e308, 1e308}));
    EXPECT_FALSE(VwapSchedule::create({1.0, 0, 400, 0}, {}));
}

TEST(Vwap, ReleasesCumulativeWeightShare) {
    const auto schedule = VwapSchedule::create(kParams, {1.0, 3.0, 4.0, 2.0});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(0, kNoMarket), 0.1);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(100, kNoMarket), 0.4);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(200, kNoMarket), 0.8);
    EXPECT_EQ(schedule->target_qty_at(300, kNoMarket), 1.0);
}

TEST(Vwap, NothingReleasedBeforeStart) {
    const auto schedule = VwapSchedule::create({1.0, 1000, 400, 4}, {1.0, 1.0, 1.0, 1.0});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(999, kNoMarket), 0.0);
}

TEST(Vwap, EqualWeightsMatchTwap) {
    const auto vwap = VwapSchedule::create(kParams, {5.0, 5.0, 5.0, 5.0});
    const auto twap = TwapSchedule::create(kParams);
    ASSERT_TRUE(vwap);
    ASSERT_TRUE(twap);
    for (const std::int64_t t : {0, 100, 200, 300}) {
        EXPECT_NEAR(vwap->target_qty_at(t, kNoMarket), twap->target_qty_at(t, kNoMarket), 1e-12);
    }
}

TEST(Vwap, ReportsName) {
    const auto schedule = VwapSchedule::create(kParams, {1.0, 1.0, 1.0, 1.0});
    ASSERT_TRUE(schedule);
    EXPECT_STREQ(schedule->name(), "vwap");
}
```

- [ ] **Step 2: Build and confirm the failure** (`vwap.h` not found).

- [ ] **Step 3: Implement `engine/src/vwap.h` and `engine/src/vwap.cpp`**

```cpp
#pragma once

#include <optional>
#include <vector>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class VwapSchedule final : public Schedule {
public:
    static std::optional<VwapSchedule> create(const SliceParams& params,
                                              const std::vector<double>& weights);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "vwap"; }

private:
    VwapSchedule(const SliceParams& params, std::vector<double> cumulative_fraction);

    SliceParams params_;
    std::vector<double> cumulative_fraction_;
};

}  // namespace slipstream
```

```cpp
#include "vwap.h"

#include <cmath>
#include <cstddef>
#include <utility>

namespace slipstream {

std::optional<VwapSchedule> VwapSchedule::create(const SliceParams& params,
                                                 const std::vector<double>& weights) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (weights.size() != static_cast<std::size_t>(params.num_slices)) return std::nullopt;
    double total = 0.0;
    for (const double weight : weights) {
        if (!std::isfinite(weight) || weight <= 0.0) return std::nullopt;
        total += weight;
    }
    if (!std::isfinite(total)) return std::nullopt;
    std::vector<double> cumulative;
    cumulative.reserve(weights.size());
    double running = 0.0;
    for (const double weight : weights) {
        running += weight;
        cumulative.push_back(running / total);
    }
    return VwapSchedule(params, std::move(cumulative));
}

VwapSchedule::VwapSchedule(const SliceParams& params, std::vector<double> cumulative_fraction)
    : params_(params), cumulative_fraction_(std::move(cumulative_fraction)) {}

double VwapSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const std::int64_t released = released_slices(params_, now_ns);
    if (released == 0) return 0.0;
    if (released == params_.num_slices) return params_.total_qty;
    return params_.total_qty * cumulative_fraction_[static_cast<std::size_t>(released - 1)];
}

}  // namespace slipstream
```

- [ ] **Step 4: Build, run ctest (all pass), and commit:** `feat(engine): add VWAP schedule driven by per-slice volume weights`.

### Task E3: Almgren-Chriss schedule

**Files:** Create `engine/src/almgren_chriss.h`, `engine/src/almgren_chriss.cpp`, and `engine/tests/almgren_chriss_test.cpp`, and add them to CMake.

- [ ] **Step 1: Write the test `engine/tests/almgren_chriss_test.cpp`**

With σ = η = τ = 1, the κ equation becomes `cosh(κτ) = 1 + λ/2`, so `lambda_for(κτ) = 2(cosh(κτ) − 1)` yields an exact, known κ.

```cpp
#include "almgren_chriss.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <limits>

#include "twap.h"

using namespace slipstream;

namespace {
const MarketState kNoMarket{0.0};
constexpr std::int64_t kSec = 1'000'000'000;
constexpr SliceParams kParams{1.0, 0, 4 * kSec, 4};

double lambda_for(double kappa_tau) { return 2.0 * (std::cosh(kappa_tau) - 1.0); }
}  // namespace

TEST(AlmgrenChriss, RejectsInvalidParams) {
    const double nan = std::nan("");
    const double inf = std::numeric_limits<double>::infinity();
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, 0.0, 1.0, 1.0));
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, 1.0, 0.0, 1.0));
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, 0.0));
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, nan, 1.0, 1.0));
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, 1.0, inf, 1.0));
    EXPECT_FALSE(AlmgrenChrissSchedule::create(kParams, 1e300, 1e-300, 1e300));
    EXPECT_FALSE(AlmgrenChrissSchedule::create({1.0, 0, 4 * kSec, 0}, 1.0, 1.0, 1.0));
}

TEST(AlmgrenChriss, TinyRiskAversionMatchesTwap) {
    const auto ac = AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, 1e-30);
    const auto twap = TwapSchedule::create(kParams);
    ASSERT_TRUE(ac);
    ASSERT_TRUE(twap);
    EXPECT_NEAR(ac->kappa_horizon(), 0.0, 1e-9);
    for (const std::int64_t t : {std::int64_t{0}, kSec, 2 * kSec, 3 * kSec}) {
        EXPECT_NEAR(ac->target_qty_at(t, kNoMarket), twap->target_qty_at(t, kNoMarket), 1e-9);
    }
}

TEST(AlmgrenChriss, MatchesClosedFormTrajectory) {
    const auto ac = AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, lambda_for(0.5));
    ASSERT_TRUE(ac);
    EXPECT_NEAR(ac->kappa_horizon(), 2.0, 1e-9);
    for (int k = 1; k <= 3; ++k) {
        const double expected = 1.0 - std::sinh(0.5 * (4 - k)) / std::sinh(2.0);
        EXPECT_NEAR(ac->target_qty_at((k - 1) * kSec, kNoMarket), expected, 1e-9) << k;
    }
}

TEST(AlmgrenChriss, FrontLoadsRelativeToTwap) {
    const auto ac = AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, lambda_for(0.5));
    const auto twap = TwapSchedule::create(kParams);
    ASSERT_TRUE(ac);
    ASSERT_TRUE(twap);
    for (const std::int64_t t : {std::int64_t{0}, kSec, 2 * kSec}) {
        EXPECT_GT(ac->target_qty_at(t, kNoMarket), twap->target_qty_at(t, kNoMarket));
    }
}

TEST(AlmgrenChriss, CompletesExactlyAtLastSlice) {
    const auto ac = AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, lambda_for(0.5));
    ASSERT_TRUE(ac);
    EXPECT_EQ(ac->target_qty_at(3 * kSec, kNoMarket), 1.0);
    EXPECT_DOUBLE_EQ(ac->target_qty_at(-1, kNoMarket), 0.0);
}

TEST(AlmgrenChriss, HugeUrgencyStaysFiniteAndBounded) {
    const SliceParams params{1.0, 0, 10 * kSec, 10};
    const auto ac = AlmgrenChrissSchedule::create(params, 1.0, 1.0, lambda_for(70.0));
    ASSERT_TRUE(ac);
    EXPECT_NEAR(ac->kappa_horizon(), 700.0, 1e-6);
    for (int s = 0; s < 10; ++s) {
        const double v = ac->target_qty_at(s * kSec, kNoMarket);
        EXPECT_TRUE(std::isfinite(v));
        EXPECT_GE(v, 0.0);
        EXPECT_LE(v, 1.0);
    }
}

TEST(AlmgrenChriss, ReportsName) {
    const auto ac = AlmgrenChrissSchedule::create(kParams, 1.0, 1.0, 1.0);
    ASSERT_TRUE(ac);
    EXPECT_STREQ(ac->name(), "almgren_chriss");
}
```

- [ ] **Step 2: Build and confirm the failure** (`almgren_chriss.h` not found).

- [ ] **Step 3: Implement `engine/src/almgren_chriss.h` and `engine/src/almgren_chriss.cpp`**

```cpp
#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

// Almgren-Chriss optimal liquidation: linear temporary impact (eta), no permanent impact,
// risk aversion lambda. Units: sigma in price/sqrt(s), eta in price*s/qty^2, lambda per price.
class AlmgrenChrissSchedule final : public Schedule {
public:
    static std::optional<AlmgrenChrissSchedule> create(const SliceParams& params, double sigma,
                                                       double eta, double risk_aversion);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "almgren_chriss"; }

    // ~0 behaves like TWAP; larger values front-load execution.
    double kappa_horizon() const { return kappa_ * horizon_s_; }

private:
    AlmgrenChrissSchedule(const SliceParams& params, double kappa, double tau_s);
    double remaining_fraction(double t_s) const;

    SliceParams params_;
    double kappa_;
    double tau_s_;
    double horizon_s_;
};

}  // namespace slipstream
```

```cpp
#include "almgren_chriss.h"

#include <cmath>

namespace slipstream {
namespace {

constexpr double kLinearThreshold = 1e-9;

bool positive_finite(double value) { return std::isfinite(value) && value > 0.0; }

}  // namespace

std::optional<AlmgrenChrissSchedule> AlmgrenChrissSchedule::create(const SliceParams& params,
                                                                   double sigma, double eta,
                                                                   double risk_aversion) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (!positive_finite(sigma) || !positive_finite(eta) || !positive_finite(risk_aversion)) {
        return std::nullopt;
    }
    const double tau_s = static_cast<double>(params.duration_ns / params.num_slices) / 1e9;
    const double cosh_kappa_tau = 1.0 + risk_aversion * sigma * sigma * tau_s * tau_s / (2.0 * eta);
    if (!std::isfinite(cosh_kappa_tau)) return std::nullopt;
    const double kappa = std::acosh(cosh_kappa_tau) / tau_s;
    if (!std::isfinite(kappa)) return std::nullopt;
    return AlmgrenChrissSchedule(params, kappa, tau_s);
}

AlmgrenChrissSchedule::AlmgrenChrissSchedule(const SliceParams& params, double kappa,
                                             double tau_s)
    : params_(params),
      kappa_(kappa),
      tau_s_(tau_s),
      horizon_s_(tau_s * static_cast<double>(params.num_slices)) {}

// x(t)/X = sinh(k(T-t))/sinh(kT), rewritten with only non-positive exponents so it cannot overflow.
double AlmgrenChrissSchedule::remaining_fraction(double t_s) const {
    if (kappa_horizon() < kLinearThreshold) return 1.0 - t_s / horizon_s_;
    const double numerator = std::exp(-kappa_ * t_s) - std::exp(-kappa_ * (2.0 * horizon_s_ - t_s));
    const double denominator = 1.0 - std::exp(-2.0 * kappa_ * horizon_s_);
    return numerator / denominator;
}

double AlmgrenChrissSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const std::int64_t released = released_slices(params_, now_ns);
    if (released == 0) return 0.0;
    if (released == params_.num_slices) return params_.total_qty;
    const double t_s = static_cast<double>(released) * tau_s_;
    return params_.total_qty * (1.0 - remaining_fraction(t_s));
}

}  // namespace slipstream
```

- [ ] **Step 4: Build, run ctest (all pass), and commit:** `feat(engine): add Almgren-Chriss schedule with overflow-safe closed form`.

### Task E4: POV schedule + cross-schedule invariants

**Files:** Create `engine/src/pov.h`, `engine/src/pov.cpp`, `engine/tests/pov_test.cpp`, and `engine/tests/schedule_invariants_test.cpp`, and add them to CMake.

- [ ] **Step 1: Write the tests**

`engine/tests/pov_test.cpp`:
```cpp
#include "pov.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

using namespace slipstream;

namespace {
constexpr std::int64_t kSec = 1'000'000'000;
constexpr SliceParams kParams{5.0, 0, 10 * kSec, 1};
}  // namespace

TEST(Pov, RejectsInvalidParams) {
    EXPECT_FALSE(PovSchedule::create(kParams, 0.0, 0.0));
    EXPECT_FALSE(PovSchedule::create(kParams, 0.51, 0.0));
    EXPECT_FALSE(PovSchedule::create(kParams, std::nan(""), 0.0));
    EXPECT_FALSE(PovSchedule::create(kParams, 0.1, -1.0));
    EXPECT_FALSE(PovSchedule::create(kParams, 0.1, std::nan("")));
    EXPECT_FALSE(PovSchedule::create({5.0, 0, 0, 1}, 0.1, 0.0));
    EXPECT_TRUE(PovSchedule::create(kParams, PovSchedule::kMaxParticipation, 0.0));
}

TEST(Pov, TracksShareOfVolumeSinceSubmit) {
    const auto pov = PovSchedule::create(kParams, 0.1, 10.0);
    ASSERT_TRUE(pov);
    EXPECT_DOUBLE_EQ(pov->target_qty_at(kSec, MarketState{10.0}), 0.0);
    EXPECT_DOUBLE_EQ(pov->target_qty_at(kSec, MarketState{30.0}), 2.0);
    EXPECT_DOUBLE_EQ(pov->target_qty_at(kSec, MarketState{1000.0}), 5.0);
}

TEST(Pov, NothingBeforeStart) {
    const auto pov = PovSchedule::create({5.0, kSec, 10 * kSec, 1}, 0.1, 0.0);
    ASSERT_TRUE(pov);
    EXPECT_DOUBLE_EQ(pov->target_qty_at(kSec - 1, MarketState{100.0}), 0.0);
}

TEST(Pov, ExpiresAtDeadline) {
    const auto pov = PovSchedule::create(kParams, 0.1, 0.0);
    ASSERT_TRUE(pov);
    EXPECT_FALSE(pov->expired(10 * kSec - 1));
    EXPECT_TRUE(pov->expired(10 * kSec));
    EXPECT_FALSE(pov->expired(-1));
    EXPECT_STREQ(pov->name(), "pov");
}
```

`engine/tests/schedule_invariants_test.cpp`:
```cpp
#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <random>
#include <vector>

#include "almgren_chriss.h"
#include "pov.h"
#include "twap.h"
#include "vwap.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

void expect_monotone_and_bounded(const Schedule& schedule, double total, std::mt19937& rng) {
    std::uniform_int_distribution<std::int64_t> time_dist(-kSec, 30 * kSec);
    std::uniform_real_distribution<double> volume_step(0.0, 5.0);
    std::vector<std::int64_t> times(200);
    for (auto& t : times) t = time_dist(rng);
    std::sort(times.begin(), times.end());
    double volume = 0.0;
    double previous = 0.0;
    for (const auto t : times) {
        volume += volume_step(rng);
        const double target = schedule.target_qty_at(t, MarketState{volume});
        EXPECT_GE(target, previous - 1e-12) << schedule.name();
        EXPECT_GE(target, 0.0) << schedule.name();
        EXPECT_LE(target, total + 1e-12) << schedule.name();
        previous = target;
    }
}

}  // namespace

TEST(ScheduleInvariants, AllSchedulesAreMonotoneAndBounded) {
    std::mt19937 rng(42);
    std::uniform_int_distribution<std::int32_t> slices_dist(1, 50);
    std::uniform_real_distribution<double> positive(0.01, 10.0);
    for (int trial = 0; trial < 50; ++trial) {
        const std::int32_t slices = slices_dist(rng);
        const SliceParams params{positive(rng), 0, 20 * kSec, slices};
        std::vector<double> weights(static_cast<std::size_t>(slices));
        for (auto& w : weights) w = positive(rng);
        const auto twap = TwapSchedule::create(params);
        const auto vwap = VwapSchedule::create(params, weights);
        const auto ac =
            AlmgrenChrissSchedule::create(params, positive(rng), positive(rng), positive(rng));
        const auto pov = PovSchedule::create(params, 0.25, 0.0);
        ASSERT_TRUE(twap && vwap && ac && pov);
        expect_monotone_and_bounded(*twap, params.total_qty, rng);
        expect_monotone_and_bounded(*vwap, params.total_qty, rng);
        expect_monotone_and_bounded(*ac, params.total_qty, rng);
        expect_monotone_and_bounded(*pov, params.total_qty, rng);
    }
}
```

- [ ] **Step 2: Build and confirm the failure** (`pov.h` not found).

- [ ] **Step 3: Implement `engine/src/pov.h` and `engine/src/pov.cpp`**

```cpp
#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class PovSchedule final : public Schedule {
public:
    static constexpr double kMaxParticipation = 0.5;

    static std::optional<PovSchedule> create(const SliceParams& params, double participation,
                                             double volume_at_submit);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    bool expired(std::int64_t now_ns) const override;
    const char* name() const override { return "pov"; }

private:
    PovSchedule(const SliceParams& params, double participation, double volume_at_submit);

    SliceParams params_;
    double participation_;
    double volume_at_submit_;
};

}  // namespace slipstream
```

```cpp
#include "pov.h"

#include <algorithm>
#include <cmath>

namespace slipstream {

std::optional<PovSchedule> PovSchedule::create(const SliceParams& params, double participation,
                                               double volume_at_submit) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (!std::isfinite(participation) || participation <= 0.0 ||
        participation > kMaxParticipation) {
        return std::nullopt;
    }
    if (!std::isfinite(volume_at_submit) || volume_at_submit < 0.0) return std::nullopt;
    return PovSchedule(params, participation, volume_at_submit);
}

PovSchedule::PovSchedule(const SliceParams& params, double participation, double volume_at_submit)
    : params_(params), participation_(participation), volume_at_submit_(volume_at_submit) {}

double PovSchedule::target_qty_at(std::int64_t now_ns, const MarketState& market) const {
    if (now_ns < params_.start_ns) return 0.0;
    const double traded = market.cumulative_volume - volume_at_submit_;
    if (!(traded > 0.0)) return 0.0;
    return std::min(params_.total_qty, participation_ * traded);
}

bool PovSchedule::expired(std::int64_t now_ns) const {
    return now_ns >= params_.start_ns && now_ns - params_.start_ns >= params_.duration_ns;
}

}  // namespace slipstream
```

- [ ] **Step 4: Build, run ctest (all pass), and commit:** `feat(engine): add POV schedule and cross-schedule invariant test`.

### Task E5: Schedule specs, factory, and engine integration (trades, deadline, algo)

**Files:**
- Create: `engine/src/schedule_spec.h`, `engine/src/schedule_factory.h`, `engine/src/schedule_factory.cpp`, `engine/tests/engine_schedules_test.cpp`
- Modify: `engine/src/types.h` (add `Trade`), `engine/src/engine.h`, `engine/src/engine.cpp`, CMake

- [ ] **Step 1: Write the test `engine/tests/engine_schedules_test.cpp`**

```cpp
#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

class EngineSchedulesTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 50.0}}, {{101.0, 50.0}}));
    }
};

}  // namespace

TEST_F(EngineSchedulesTest, DefaultScheduleIsTwap) {
    ASSERT_TRUE(engine.submit({"t", Side::Buy, 1.0, 0, kSec, 1}).accepted);
    EXPECT_EQ(engine.statuses().at(0).algo, "twap");
}

TEST_F(EngineSchedulesTest, VwapOrderFollowsWeights) {
    ASSERT_TRUE(
        engine.submit({"v", Side::Buy, 10.0, 0, 4 * kSec, 4}, VwapSpec{{1.0, 3.0, 4.0, 2.0}})
            .accepted);
    EXPECT_DOUBLE_EQ(engine.step(0).at(0).qty, 1.0);
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 3.0);
    EXPECT_EQ(engine.statuses().at(0).algo, "vwap");
}

TEST_F(EngineSchedulesTest, AlmgrenChrissOrderFrontLoads) {
    const double lambda = 2.0 * (std::cosh(0.5) - 1.0);
    ASSERT_TRUE(engine.submit({"ac", Side::Buy, 1.0, 0, 4 * kSec, 4},
                              AlmgrenChrissSpec{1.0, 1.0, lambda})
                    .accepted);
    EXPECT_NEAR(engine.step(0).at(0).qty, 1.0 - std::sinh(1.5) / std::sinh(2.0), 1e-9);
    EXPECT_EQ(engine.statuses().at(0).algo, "almgren_chriss");
}

TEST_F(EngineSchedulesTest, PovOrderTradesShareOfVolumeSinceSubmit) {
    ASSERT_TRUE(engine.apply_trades({{100.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"p", Side::Buy, 10.0, 0, 10 * kSec, 1}, PovSpec{0.2}).accepted);
    EXPECT_TRUE(engine.step(0).empty());
    ASSERT_TRUE(engine.apply_trades({{100.0, 10.0}, {100.0, 5.0}}));
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 3.0);
    EXPECT_EQ(engine.statuses().at(0).algo, "pov");
}

TEST_F(EngineSchedulesTest, PovOrderHaltsAtDeadline) {
    ASSERT_TRUE(engine.submit({"p", Side::Buy, 10.0, 0, 2 * kSec, 1}, PovSpec{0.2}).accepted);
    ASSERT_TRUE(engine.apply_trades({{100.0, 5.0}}));
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 1.0);
    EXPECT_TRUE(engine.step(2 * kSec).empty());
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "deadline reached");
    EXPECT_DOUBLE_EQ(status.filled_qty, 1.0);
}

TEST_F(EngineSchedulesTest, RejectsInvalidScheduleSpecs) {
    EXPECT_EQ(engine.submit({"v", Side::Buy, 1.0, 0, 4 * kSec, 4}, VwapSpec{{1.0}}).reason,
              "invalid schedule");
    EXPECT_EQ(engine.submit({"p", Side::Buy, 1.0, 0, kSec, 1}, PovSpec{0.9}).reason,
              "invalid schedule");
    EXPECT_EQ(engine.submit({"a", Side::Buy, 1.0, 0, kSec, 1}, AlmgrenChrissSpec{0.0, 1.0, 1.0})
                  .reason,
              "invalid schedule");
}

TEST_F(EngineSchedulesTest, ApplyTradesValidatesAndAccumulates) {
    EXPECT_FALSE(engine.apply_trades({{100.0, -1.0}}));
    EXPECT_FALSE(engine.apply_trades({{std::nan(""), 1.0}}));
    EXPECT_FALSE(engine.apply_trades({{100.0, 1.0}, {0.0, 1.0}}));
    EXPECT_DOUBLE_EQ(engine.market_volume(), 0.0);
    EXPECT_TRUE(engine.apply_trades({{100.0, 2.0}, {100.0, 3.0}}));
    EXPECT_DOUBLE_EQ(engine.market_volume(), 5.0);
}
```

- [ ] **Step 2: Build and confirm the failure** (`VwapSpec` etc. undeclared).

- [ ] **Step 3: Add `Trade` to `engine/src/types.h`**, after `Level`:
```cpp
struct Trade {
    double price;
    double qty;
};
```

- [ ] **Step 4: Create `engine/src/schedule_spec.h`**
```cpp
#pragma once

#include <variant>
#include <vector>

namespace slipstream {

struct TwapSpec {};

struct VwapSpec {
    std::vector<double> weights;
};

struct AlmgrenChrissSpec {
    double sigma;
    double eta;
    double risk_aversion;
};

struct PovSpec {
    double participation;
};

using ScheduleSpec = std::variant<TwapSpec, VwapSpec, AlmgrenChrissSpec, PovSpec>;

}  // namespace slipstream
```

- [ ] **Step 5: Create `engine/src/schedule_factory.h` and `engine/src/schedule_factory.cpp`**
```cpp
#pragma once

#include <memory>

#include "schedule.h"
#include "schedule_spec.h"
#include "slicing.h"

namespace slipstream {

// nullptr when the parameters are invalid for the requested schedule.
std::unique_ptr<Schedule> make_schedule(const SliceParams& params, const ScheduleSpec& spec,
                                        const MarketState& at_submit);

}  // namespace slipstream
```
```cpp
#include "schedule_factory.h"

#include <optional>
#include <utility>

#include "almgren_chriss.h"
#include "pov.h"
#include "twap.h"
#include "vwap.h"

namespace slipstream {
namespace {

template <typename T>
std::unique_ptr<Schedule> own(std::optional<T> schedule) {
    if (!schedule) return nullptr;
    return std::make_unique<T>(std::move(*schedule));
}

struct Builder {
    const SliceParams& params;
    const MarketState& at_submit;

    std::unique_ptr<Schedule> operator()(const TwapSpec&) const {
        return own(TwapSchedule::create(params));
    }
    std::unique_ptr<Schedule> operator()(const VwapSpec& spec) const {
        return own(VwapSchedule::create(params, spec.weights));
    }
    std::unique_ptr<Schedule> operator()(const AlmgrenChrissSpec& spec) const {
        return own(AlmgrenChrissSchedule::create(params, spec.sigma, spec.eta, spec.risk_aversion));
    }
    std::unique_ptr<Schedule> operator()(const PovSpec& spec) const {
        return own(PovSchedule::create(params, spec.participation, at_submit.cumulative_volume));
    }
};

}  // namespace

std::unique_ptr<Schedule> make_schedule(const SliceParams& params, const ScheduleSpec& spec,
                                        const MarketState& at_submit) {
    return std::visit(Builder{params, at_submit}, spec);
}

}  // namespace slipstream
```

- [ ] **Step 6: Update `engine/src/engine.h`**
  - Replace `#include "twap.h"` with `#include <memory>`, `#include "schedule.h"` and `#include "schedule_spec.h"`.
  - Add `std::string algo;` as the last member of `OrderStatus`.
  - Change the public API to:
```cpp
    SubmitResult submit(const ParentOrderRequest& request, const ScheduleSpec& spec = TwapSpec{});
    bool apply_trades(const std::vector<Trade>& trades);
    double market_volume() const;
```
  - In `ParentOrder`, replace `TwapSchedule schedule;` with `std::unique_ptr<Schedule> schedule;`.
  - Add private members:
```cpp
    void advance_locked(ParentOrder& order, std::int64_t now_ns, std::optional<double> ref_price,
                        const MarketState& market, std::vector<Fill>& fills);

    double market_volume_ = 0.0;
```

- [ ] **Step 7: Update `engine/src/engine.cpp`**
  - Add `#include <cmath>`, `#include <memory>` and `#include "schedule_factory.h"`.
  - In `submit`, replace the `TwapSchedule::create` block with:
```cpp
    auto schedule = make_schedule(
        {request.qty, request.start_ns, request.duration_ns, request.num_slices}, spec,
        MarketState{market_volume_});
    if (!schedule) return {false, "invalid schedule"};
```
    and push `ParentOrder{request, std::move(schedule), OrderState::Working, 0.0, 0.0, *arrival_mid, cost_bps(...), ""}`.
  - Add:
```cpp
bool Engine::apply_trades(const std::vector<Trade>& trades) {
    std::lock_guard lock(mu_);
    double added = 0.0;
    for (const auto& trade : trades) {
        if (!std::isfinite(trade.price) || !std::isfinite(trade.qty)) return false;
        if (trade.price <= 0.0 || trade.qty <= 0.0) return false;
        added += trade.qty;
    }
    if (!std::isfinite(market_volume_ + added)) return false;
    market_volume_ += added;
    return true;
}

double Engine::market_volume() const {
    std::lock_guard lock(mu_);
    return market_volume_;
}
```
  - Replace `step` with the loop below, and move the old per-order body into `advance_locked`, using `return` where it previously used `continue`:
```cpp
std::vector<Fill> Engine::step(std::int64_t now_ns) {
    std::lock_guard lock(mu_);
    std::vector<Fill> fills;
    const auto ref_price = book_.mid();
    const MarketState market{market_volume_};
    for (auto& order : orders_) {
        if (order.state != OrderState::Working) continue;
        advance_locked(order, now_ns, ref_price, market, fills);
        if (order.state == OrderState::Working && order.schedule->expired(now_ns)) {
            order.state = OrderState::Halted;
            order.halt_reason = "deadline reached";
        }
    }
    return fills;
}

void Engine::advance_locked(ParentOrder& order, std::int64_t now_ns,
                            std::optional<double> ref_price, const MarketState& market,
                            std::vector<Fill>& fills) {
    const double dust = order.request.qty * kDustFraction;
    const double child = order.schedule->target_qty_at(now_ns, market) - order.filled_qty;
    if (child <= dust || !ref_price) return;

    const auto decision = risk_.check_child(order.request.side, child, *ref_price, position_,
                                            order.filled_notional);
    if (!decision.ok) {
        order.state = OrderState::Halted;
        order.halt_reason = decision.reason;
        return;
    }

    const auto result = simulate_market_fill(book_.liquidity_for(order.request.side), child);
    if (result.filled_qty <= 0.0) return;

    order.filled_qty += result.filled_qty;
    order.filled_notional += result.filled_qty * result.avg_price;
    position_ += signed_qty(order.request.side, result.filled_qty);
    fills.push_back({order.request.order_id, now_ns, result.filled_qty, result.avg_price});
    if (order.request.qty - order.filled_qty <= dust) order.state = OrderState::Completed;
}
```
  - In `statuses()`, append `order.schedule->name()` as the final `algo` field of each pushed `OrderStatus`.
  - Add `src/schedule_factory.cpp` to `slipstream_core` and `tests/engine_schedules_test.cpp` to `engine_tests`.

- [ ] **Step 8: Build and run all ctest.** Every existing engine test plus the 7 new ones must pass. Commit: `feat(engine): select schedules per order, track market volume, halt expired POV`.

### Task E6: Proto + gRPC service

**Files:** Modify `proto/slipstream/v1/execution.proto`, `engine/src/service.h`, `engine/src/service.cpp` and `engine/tests/service_test.cpp`.

- [ ] **Step 1: Extend the proto**
  - Add these messages before `ParentOrder`:
```proto
message TwapParams {}

message VwapParams {
  repeated double weights = 1;
}

message AlmgrenChrissParams {
  double sigma = 1;
  double eta = 2;
  double risk_aversion = 3;
}

message PovParams {
  double participation = 1;
}
```
  - Inside `ParentOrder`, after field 6, add:
```proto
  oneof schedule {
    TwapParams twap = 7;
    VwapParams vwap = 8;
    AlmgrenChrissParams almgren_chriss = 9;
    PovParams pov = 10;
  }
```
  - Add to `OrderStatus`: `string algo = 11;`
  - Add these messages:
```proto
message Trade {
  double price = 1;
  double qty = 2;
  int64 ts_ns = 3;
}

message TradeBatch {
  string symbol = 1;
  repeated Trade trades = 2;
}

message TradeAck {}
```
  - Add to the service: `rpc ApplyTrades(TradeBatch) returns (TradeAck);`

- [ ] **Step 2: Add the failing tests to `engine/tests/service_test.cpp`** (inside the existing file, reusing `ServiceTest`, `snapshot()` and `order()`):
```cpp
TEST_F(ServiceTest, VwapScheduleMapsThroughOneof) {
    const auto update = snapshot();
    v1::BookAck ack;
    ASSERT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());
    auto request = order(v1::SIDE_BUY);
    request.set_num_slices(2);
    request.mutable_vwap()->add_weights(1.0);
    request.mutable_vwap()->add_weights(3.0);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &request, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::StatusRequest status_request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &status_request, &status).ok());
    EXPECT_EQ(status.orders(0).algo(), "vwap");
}

TEST_F(ServiceTest, TooManyVwapWeightsIsInvalidArgument) {
    auto request = order(v1::SIDE_BUY);
    for (int i = 0; i <= kMaxSlices; ++i) request.mutable_vwap()->add_weights(1.0);
    v1::SubmitReply reply;
    EXPECT_EQ(service.SubmitParentOrder(nullptr, &request, &reply).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, ApplyTradesValidatesBoundary) {
    v1::TradeAck ack;
    v1::TradeBatch wrong_symbol;
    wrong_symbol.set_symbol("ETH/USD");
    EXPECT_EQ(service.ApplyTrades(nullptr, &wrong_symbol, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch too_many;
    too_many.set_symbol("BTC/USD");
    for (int i = 0; i <= ExecutionService::kMaxTradesPerBatch; ++i) {
        auto* trade = too_many.add_trades();
        trade->set_price(100.0);
        trade->set_qty(1.0);
    }
    EXPECT_EQ(service.ApplyTrades(nullptr, &too_many, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch bad;
    bad.set_symbol("BTC/USD");
    auto* negative = bad.add_trades();
    negative->set_price(100.0);
    negative->set_qty(-1.0);
    EXPECT_EQ(service.ApplyTrades(nullptr, &bad, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch good;
    good.set_symbol("BTC/USD");
    auto* trade = good.add_trades();
    trade->set_price(100.0);
    trade->set_qty(2.5);
    EXPECT_TRUE(service.ApplyTrades(nullptr, &good, &ack).ok());
    EXPECT_DOUBLE_EQ(engine.market_volume(), 2.5);
}

TEST_F(ServiceTest, PovOrderFillsFromReportedTrades) {
    const auto update = snapshot();
    v1::BookAck book_ack;
    ASSERT_TRUE(service.ApplyBookUpdate(nullptr, &update, &book_ack).ok());
    auto request = order(v1::SIDE_BUY);
    request.mutable_pov()->set_participation(0.5);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &request, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::TradeBatch trades;
    trades.set_symbol("BTC/USD");
    auto* trade = trades.add_trades();
    trade->set_price(100.0);
    trade->set_qty(1.0);
    v1::TradeAck trade_ack;
    ASSERT_TRUE(service.ApplyTrades(nullptr, &trades, &trade_ack).ok());
    v1::StepRequest step;
    step.set_now_ns(0);
    v1::StepReply step_reply;
    ASSERT_TRUE(service.Step(nullptr, &step, &step_reply).ok());
    ASSERT_EQ(step_reply.fills_size(), 1);
    EXPECT_DOUBLE_EQ(step_reply.fills(0).qty(), 0.5);
}
```
Also add `EXPECT_EQ(status.orders(0).algo(), "twap");` to the existing `SubmitStepAndStatusRoundTrip`, and add `#include "slicing.h"` for `kMaxSlices`.

- [ ] **Step 3: Build and confirm the failure** (`ApplyTrades` / `kMaxTradesPerBatch` missing).

- [ ] **Step 4: Update `engine/src/service.h`**
  - Add `static constexpr int kMaxTradesPerBatch = 1000;`.
  - Declare `grpc::Status ApplyTrades(grpc::ServerContext*, const v1::TradeBatch* request, v1::TradeAck*) override;`.

- [ ] **Step 5: Update `engine/src/service.cpp`**
  - Add `#include "schedule_spec.h"` and `#include "slicing.h"`.
  - Add this to the anonymous namespace:
```cpp
std::optional<ScheduleSpec> schedule_from_proto(const v1::ParentOrder& order) {
    switch (order.schedule_case()) {
        case v1::ParentOrder::SCHEDULE_NOT_SET:
        case v1::ParentOrder::kTwap:
            return TwapSpec{};
        case v1::ParentOrder::kVwap: {
            const auto& weights = order.vwap().weights();
            if (weights.size() > kMaxSlices) return std::nullopt;
            return VwapSpec{std::vector<double>(weights.begin(), weights.end())};
        }
        case v1::ParentOrder::kAlmgrenChriss:
            return AlmgrenChrissSpec{order.almgren_chriss().sigma(), order.almgren_chriss().eta(),
                                     order.almgren_chriss().risk_aversion()};
        case v1::ParentOrder::kPov:
            return PovSpec{order.pov().participation()};
    }
    return std::nullopt;
}
```
  - In `SubmitParentOrder`, after the side check:
```cpp
    const auto spec = schedule_from_proto(*request);
    if (!spec) return invalid("too many vwap weights");
```
    and pass `*spec` as the second argument of `engine_.submit(...)`.
  - In `GetStatus`, add `out->set_algo(status.algo);`.
  - Add:
```cpp
grpc::Status ExecutionService::ApplyTrades(grpc::ServerContext*, const v1::TradeBatch* request,
                                           v1::TradeAck*) {
    if (request->symbol() != symbol_) return invalid("unexpected symbol");
    if (request->trades_size() > kMaxTradesPerBatch) return invalid("too many trades");
    std::vector<Trade> trades;
    trades.reserve(static_cast<std::size_t>(request->trades_size()));
    for (const auto& trade : request->trades()) trades.push_back({trade.price(), trade.qty()});
    if (!engine_.apply_trades(trades)) return invalid("invalid trade");
    return grpc::Status::OK;
}
```

- [ ] **Step 6: Run the full gate:** `bash scripts/ci.sh` → `CI OK`. The Python suite must still pass, because the proto changes are additive. Commit: `feat(engine): expose schedule selection and ApplyTrades over gRPC`.

- [ ] **Step 7: Track E finish.** Run `/security-review` on the branch, then superpowers:receiving-code-review, then verification-before-completion. Push the branch and open PR 1 (publishing is approved for this project; merge once CI is green).

---

# Track P — Python data (PR 2)

All Python commands run from `python/`. Keep `mypy --strict` and `ruff` clean after every task.

### Task P1: Kraken OHLC REST client

**Files:** Create `python/slipstream/kraken_rest.py` and `python/tests/test_kraken_rest.py`.

- [ ] **Step 1: Write the failing test `python/tests/test_kraken_rest.py`**

```python
import json
import urllib.error
from typing import Any

import pytest

from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import (
    MAX_RESPONSE_BYTES,
    Bar,
    KrakenRestError,
    fetch_ohlc,
    parse_ohlc,
    rest_pair,
)

ROWS = [
    [1700000000, "100.0", "101.0", "99.5", "100.5", "100.2", "2.5", 10],
    [1700000060, "100.5", "100.5", "100.5", "100.5", "0.0", "0.00000000", 0],
]


def ohlc_body(rows: Any = None, errors: Any = None, extra_series: bool = False) -> bytes:
    result: dict[str, Any] = {"XXBTZUSD": ROWS if rows is None else rows, "last": 1700000060}
    if extra_series:
        result["XETHZUSD"] = []
    return json.dumps({"error": [] if errors is None else errors, "result": result}).encode()


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_parses_bars() -> None:
    bars = parse_ohlc(ohlc_body())
    assert bars == (
        Bar(1700000000, 100.0, 101.0, 99.5, 100.5, 100.2, 2.5, 10),
        Bar(1700000060, 100.5, 100.5, 100.5, 100.5, 0.0, 0.0, 0),
    )


def test_kraken_error_array_raises() -> None:
    with pytest.raises(KrakenRestError, match="Kraken error"):
        parse_ohlc(ohlc_body(errors=["EQuery:Unknown asset pair"]))


def test_rest_errors_are_kraken_message_errors() -> None:
    assert issubclass(KrakenRestError, KrakenMessageError)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[" * 100_000,
        json.dumps([1]).encode(),
        json.dumps({"error": [], "result": []}).encode(),
        ohlc_body(extra_series=True),
        ohlc_body(rows=[[1700000000, "100.0"]]),
        ohlc_body(rows=[[True, "1", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[-1, "1", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, 1.0, "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "0", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "nan", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1e400", "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1" * 65, "1", "1", "1", "1", "1", 1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "-1", 1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "1", -1]]),
        ohlc_body(rows=[[1, "1", "1", "1", "1", "1", "1", 1]] * 1001),
    ],
    ids=[
        "not-json", "deep", "not-object", "result-not-object", "two-series", "short-row",
        "bool-time", "negative-time", "number-not-string", "zero-price", "nan", "overflow",
        "long-field", "negative-volume", "negative-count", "too-many-rows",
    ],
)
def test_hostile_ohlc_raises(raw: bytes) -> None:
    with pytest.raises(KrakenRestError):
        parse_ohlc(raw)


def test_rest_pair_mapping() -> None:
    assert rest_pair("BTC/USD") == "XBTUSD"
    with pytest.raises(KrakenRestError, match="no REST pair"):
        rest_pair("DOGE/XYZ")


def test_fetch_builds_fixed_https_request() -> None:
    seen: dict[str, Any] = {}

    def opener(request: Any, timeout: float) -> FakeResponse:
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse(ohlc_body())

    assert fetch_ohlc("BTC/USD", 15, opener=opener) == ohlc_body()
    assert seen == {
        "url": "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=15",
        "timeout": 10.0,
    }


def test_fetch_rejects_oversized_response() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        return FakeResponse(b" " * (MAX_RESPONSE_BYTES + 10))

    with pytest.raises(KrakenRestError, match="too large"):
        fetch_ohlc("BTC/USD", 1, opener=opener)


def test_fetch_wraps_network_errors() -> None:
    def opener(request: Any, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("unreachable")

    with pytest.raises(KrakenRestError, match="request failed"):
        fetch_ohlc("BTC/USD", 1, opener=opener)


def test_fetch_rejects_unsupported_interval() -> None:
    with pytest.raises(KrakenRestError, match="interval"):
        fetch_ohlc("BTC/USD", 5)
```

- [ ] **Step 2: Run the test and confirm it fails** (`ModuleNotFoundError: slipstream.kraken_rest`).

- [ ] **Step 3: Implement `python/slipstream/kraken_rest.py`**

```python
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from slipstream.kraken import KrakenMessageError

OHLC_URL = "https://api.kraken.com/0/public/OHLC"
MAX_RESPONSE_BYTES = 2 << 20
MAX_BARS = 1000
MAX_FIELD_CHARS = 64
SUPPORTED_INTERVALS = (1, 15)
_REST_PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD"}


class KrakenRestError(KrakenMessageError):
    pass


@dataclass(frozen=True)
class Bar:
    time_s: int
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


def rest_pair(symbol: str) -> str:
    try:
        return _REST_PAIRS[symbol]
    except KeyError as exc:
        raise KrakenRestError(f"no REST pair mapping for {symbol!r}") from exc


def fetch_ohlc(
    symbol: str,
    interval_min: int,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout_s: float = 10.0,
) -> bytes:
    if interval_min not in SUPPORTED_INTERVALS:
        raise KrakenRestError(f"unsupported OHLC interval {interval_min}")
    query = urllib.parse.urlencode({"pair": rest_pair(symbol), "interval": interval_min})
    request = urllib.request.Request(  # noqa: S310 - fixed https URL, not user-controlled
        f"{OHLC_URL}?{query}", headers={"User-Agent": "slipstream"}
    )
    try:
        with opener(request, timeout=timeout_s) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise KrakenRestError(f"OHLC request failed: {exc}") from exc
    if not isinstance(body, bytes):
        raise KrakenRestError("OHLC response is not bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        raise KrakenRestError("OHLC response too large")
    return body


def parse_ohlc(raw: str | bytes) -> tuple[Bar, ...]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise KrakenRestError("OHLC response too large")
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise KrakenRestError("invalid OHLC JSON") from exc
    if not isinstance(msg, dict):
        raise KrakenRestError("OHLC response is not an object")
    errors = msg.get("error")
    if not isinstance(errors, list):
        raise KrakenRestError("OHLC response missing error list")
    if errors:
        raise KrakenRestError(f"Kraken error: {errors[:3]!r}")
    result = msg.get("result")
    if not isinstance(result, dict):
        raise KrakenRestError("OHLC result is not an object")
    series = [value for key, value in result.items() if key != "last"]
    if len(series) != 1 or not isinstance(series[0], list):
        raise KrakenRestError("expected exactly one OHLC series")
    rows = series[0]
    if len(rows) > MAX_BARS:
        raise KrakenRestError("too many OHLC bars")
    return tuple(_parse_bar(row) for row in rows)


def _parse_bar(row: Any) -> Bar:
    if not isinstance(row, list) or len(row) != 8:
        raise KrakenRestError("OHLC row must have 8 fields")
    return Bar(
        time_s=_non_negative_int(row[0], "time"),
        open=_decimal(row[1], "open", allow_zero=False),
        high=_decimal(row[2], "high", allow_zero=False),
        low=_decimal(row[3], "low", allow_zero=False),
        close=_decimal(row[4], "close", allow_zero=False),
        vwap=_decimal(row[5], "vwap", allow_zero=True),
        volume=_decimal(row[6], "volume", allow_zero=True),
        count=_non_negative_int(row[7], "count"),
    )


def _decimal(value: Any, name: str, *, allow_zero: bool) -> float:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise KrakenRestError(f"OHLC {name} must be a short decimal string")
    try:
        result = float(value)
    except ValueError as exc:
        raise KrakenRestError(f"OHLC {name} is not a number") from exc
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise KrakenRestError(f"OHLC {name} out of range")
    return result


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise KrakenRestError(f"OHLC {name} must be a non-negative integer")
    return value
```

- [ ] **Step 4: Run the tests (pass), then the gate. Commit:** `feat(orchestrator): add strict Kraken OHLC REST client`.

### Task P2: Calibration

**Files:** Create `python/slipstream/calibration.py` and `python/tests/test_calibration.py`.

- [ ] **Step 1: Write the failing test `python/tests/test_calibration.py`**

```python
import math
import random

import pytest

from slipstream.calibration import (
    URGENCY_RISK_AVERSION,
    CalibrationError,
    estimate_eta,
    estimate_sigma,
    vwap_weights,
)
from slipstream.kraken_rest import Bar

DAY0 = 1_700_006_400  # 00:00 UTC (multiple of 86400)
NS = 1_000_000_000


def bar(time_s: int, close: float = 100.0, volume: float = 1.0) -> Bar:
    return Bar(time_s, close, close, close, close, close, volume, 1)


def history() -> list[Bar]:
    return [
        bar(DAY0 - 86_400, volume=10.0),        # bucket 0, previous day
        bar(DAY0 - 86_400 + 900, volume=5.0),   # bucket 1, previous day
        bar(DAY0 - 2 * 86_400, volume=30.0),    # bucket 0, two days ago
    ]


def test_vwap_weights_follow_time_of_day_profile() -> None:
    assert vwap_weights(history(), DAY0 * NS, 1800, 2) == (20.0, 5.0)


def test_vwap_weights_fill_empty_buckets_with_mean_profile() -> None:
    assert vwap_weights(history(), (DAY0 + 1800) * NS, 900, 1) == (12.5,)


def test_vwap_weights_short_horizon_is_flat() -> None:
    assert vwap_weights(history(), DAY0 * NS, 60, 6) == (20.0,) * 6


@pytest.mark.parametrize(
    ("bars", "start_ns", "duration_s", "slices"),
    [([], DAY0 * NS, 60, 1), (history(), DAY0 * NS, 0, 1), (history(), DAY0 * NS, 60, 0),
     (history(), -1, 60, 1), ([bar(DAY0, volume=0.0)], DAY0 * NS, 60, 1)],
    ids=["no-bars", "zero-duration", "zero-slices", "negative-start", "zero-volume"],
)
def test_vwap_weights_reject_bad_input(
    bars: list[Bar], start_ns: int, duration_s: int, slices: int
) -> None:
    with pytest.raises(CalibrationError):
        vwap_weights(bars, start_ns, duration_s, slices)


def test_sigma_from_one_minute_closes() -> None:
    bars = [bar(DAY0 + 60 * i, close=100.0 + (i % 2)) for i in range(61)]
    random.Random(7).shuffle(bars)
    expected = math.sqrt(60 / 59) / math.sqrt(60)
    assert estimate_sigma(bars) == pytest.approx(expected)


def test_sigma_needs_enough_bars_and_movement() -> None:
    with pytest.raises(CalibrationError, match="at least"):
        estimate_sigma([bar(DAY0 + 60 * i) for i in range(10)])
    with pytest.raises(CalibrationError, match="volatility"):
        estimate_sigma([bar(DAY0 + 60 * i) for i in range(61)])


def test_eta_from_book_cost_curve() -> None:
    asks = [(100.0, 1.0), (101.0, 1.0), (102.0, 2.0)]
    # sizes 0.25, 0.5, 1, 2 cost 0, 0, 0, 0.5 above the touch; least-squares slope = 0.53125/1.796875
    assert estimate_eta(asks, slice_qty=0.5, tau_s=10.0) == pytest.approx(0.53125 / 1.796875 * 10)


def test_eta_sell_side_uses_distance_below_touch() -> None:
    bids = [(100.0, 1.0), (99.0, 1.0), (98.0, 2.0)]
    assert estimate_eta(bids, slice_qty=0.5, tau_s=10.0) == pytest.approx(0.53125 / 1.796875 * 10)


def test_eta_rejects_thin_or_flat_books() -> None:
    with pytest.raises(CalibrationError, match="thin"):
        estimate_eta([(100.0, 0.5)], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError, match="slope"):
        estimate_eta([(100.0, 100.0)], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError):
        estimate_eta([], slice_qty=0.5, tau_s=10.0)
    with pytest.raises(CalibrationError):
        estimate_eta([(100.0, 1.0)], slice_qty=0.0, tau_s=10.0)


def test_urgency_presets() -> None:
    assert URGENCY_RISK_AVERSION == {"low": 3e-6, "medium": 3e-5, "high": 3e-4}
```

- [ ] **Step 2: Run the test and confirm it fails** (module missing).

- [ ] **Step 3: Implement `python/slipstream/calibration.py`**

```python
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from slipstream.kraken_rest import Bar

SECONDS_PER_DAY = 86_400
BUCKET_S = 900
BUCKETS_PER_DAY = SECONDS_PER_DAY // BUCKET_S
MIN_SIGMA_BARS = 60
IMPACT_MULTIPLIERS = (0.5, 1.0, 2.0, 4.0)
MIN_IMPACT_POINTS = 3
URGENCY_RISK_AVERSION = {"low": 3e-6, "medium": 3e-5, "high": 3e-4}


class CalibrationError(ValueError):
    pass


def vwap_weights(
    bars_15m: Sequence[Bar], start_ns: int, duration_s: int, slices: int
) -> tuple[float, ...]:
    if start_ns < 0 or duration_s <= 0 or slices < 1:
        raise CalibrationError("invalid VWAP horizon")
    totals = [0.0] * BUCKETS_PER_DAY
    counts = [0] * BUCKETS_PER_DAY
    for bar in bars_15m:
        bucket = (bar.time_s % SECONDS_PER_DAY) // BUCKET_S
        totals[bucket] += bar.volume
        counts[bucket] += 1
    profile = [totals[i] / counts[i] if counts[i] else 0.0 for i in range(BUCKETS_PER_DAY)]
    observed = [volume for volume in profile if volume > 0]
    if not observed:
        raise CalibrationError("no volume history for VWAP profile")
    fallback = sum(observed) / len(observed)
    interval_s = duration_s / slices
    start_s = start_ns / 1e9
    weights = []
    for k in range(slices):
        midpoint_s = start_s + (k + 0.5) * interval_s
        bucket = int(midpoint_s % SECONDS_PER_DAY) // BUCKET_S
        weights.append(profile[bucket] if profile[bucket] > 0 else fallback)
    return tuple(weights)


def estimate_sigma(bars_1m: Sequence[Bar]) -> float:
    if len(bars_1m) < MIN_SIGMA_BARS:
        raise CalibrationError(f"need at least {MIN_SIGMA_BARS} one-minute bars")
    closes = [bar.close for bar in sorted(bars_1m, key=lambda b: b.time_s)]
    changes = [later - earlier for earlier, later in zip(closes, closes[1:], strict=False)]
    sigma = statistics.stdev(changes) / math.sqrt(60)
    if not math.isfinite(sigma) or sigma <= 0:
        raise CalibrationError("zero or invalid volatility estimate")
    return sigma


def estimate_eta(
    liquidity: Sequence[tuple[float, float]], slice_qty: float, tau_s: float
) -> float:
    if not liquidity:
        raise CalibrationError("empty book side")
    if not (math.isfinite(slice_qty) and slice_qty > 0 and math.isfinite(tau_s) and tau_s > 0):
        raise CalibrationError("invalid slice size or interval")
    touch = liquidity[0][0]
    sizes: list[float] = []
    costs: list[float] = []
    for multiplier in IMPACT_MULTIPLIERS:
        qty = slice_qty * multiplier
        filled, avg_price = _walk(liquidity, qty)
        if filled < qty * (1 - 1e-9):
            break
        sizes.append(qty)
        costs.append(abs(avg_price - touch))
    if len(sizes) < MIN_IMPACT_POINTS:
        raise CalibrationError("book too thin to estimate impact")
    slope = statistics.linear_regression(sizes, costs).slope
    if not math.isfinite(slope) or slope <= 0:
        raise CalibrationError("non-positive impact slope")
    return slope * tau_s


def _walk(liquidity: Sequence[tuple[float, float]], qty: float) -> tuple[float, float]:
    remaining = qty
    filled = 0.0
    notional = 0.0
    for price, level_qty in liquidity:
        if remaining <= 0:
            break
        take = min(remaining, level_qty)
        filled += take
        notional += take * price
        remaining -= take
    return filled, (notional / filled if filled > 0 else 0.0)
```

- [ ] **Step 4: Run the tests (pass) and the gate. Commit:** `feat(orchestrator): add VWAP profile, volatility, and impact calibration`.

### Task P3: Kraken trade channel

**Files:** Modify `python/slipstream/models.py`, `python/slipstream/kraken.py`, `python/slipstream/runner.py`, `python/tests/test_kraken.py` and `python/tests/test_runner.py`.

- [ ] **Step 1: Add the failing tests**

Append to `python/tests/test_kraken.py`:
```python
from slipstream.kraken import subscribe_trades_message
from slipstream.models import TradeBatch


def trade_msg(msg_type: str = "update", data: Any = None) -> str:
    rows = data if data is not None else [
        {"symbol": "BTC/USD", "side": "buy", "price": 100.5, "qty": 0.2, "ord_type": "market",
         "trade_id": 1, "timestamp": "2026-09-24T00:00:00.000000Z"},
        {"symbol": "BTC/USD", "side": "sell", "price": 100.4, "qty": 0.3, "ord_type": "limit",
         "trade_id": 2, "timestamp": "2026-09-24T00:00:00.100000Z"},
    ]
    return json.dumps({"channel": "trade", "type": msg_type, "data": rows})


def test_parses_trade_update() -> None:
    assert parse_message(trade_msg()) == TradeBatch(
        "BTC/USD", False, ((100.5, 0.2), (100.4, 0.3))
    )


def test_trade_snapshot_is_flagged() -> None:
    batch = parse_message(trade_msg("snapshot"))
    assert isinstance(batch, TradeBatch)
    assert batch.is_snapshot


@pytest.mark.parametrize(
    "raw",
    [
        trade_msg(data=[]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": 100.0, "qty": 0}]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": -1, "qty": 1}]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": 1, "qty": 1},
                        {"symbol": "ETH/USD", "price": 1, "qty": 1}]),
        trade_msg(data=[{"symbol": "BTC/USD", "price": 1, "qty": 1}] * 1001),
        trade_msg("weird"),
        json.dumps({"channel": "trade", "type": "update", "data": "x"}),
    ],
    ids=["empty", "zero-qty", "negative-price", "mixed-symbols", "too-many", "bad-type",
         "data-not-list"],
)
def test_malformed_trade_messages_raise(raw: str) -> None:
    with pytest.raises(KrakenMessageError):
        parse_message(raw)


def test_subscribe_trades_message_disables_snapshot() -> None:
    assert json.loads(subscribe_trades_message("BTC/USD")) == {
        "method": "subscribe",
        "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False},
    }
```
(Merge the new imports into the file's existing import block.)

Append to `python/tests/test_runner.py`:
```python
def test_trade_messages_do_not_touch_book_or_submit(fake_engine: FakeEngine) -> None:
    trade = json.dumps({"channel": "trade", "type": "update",
                        "data": [{"symbol": "BTC/USD", "price": 100.0, "qty": 1.0}]})
    make_runner(fake_engine).on_message(trade, 1)
    assert fake_engine.books == []
    assert fake_engine.submits == []
```

- [ ] **Step 2: Run the tests and confirm they fail** (`TradeBatch` / `subscribe_trades_message` missing).

- [ ] **Step 3: Implement**

`python/slipstream/models.py`, append:
```python
@dataclass(frozen=True)
class TradeBatch:
    symbol: str
    is_snapshot: bool
    trades: tuple[tuple[float, float], ...]
```

`python/slipstream/kraken.py`:
  - Import `TradeBatch`.
  - Add:
```python
def subscribe_trades_message(symbol: str) -> str:
    return json.dumps(
        {"method": "subscribe", "params": {"channel": "trade", "symbol": [symbol], "snapshot": False}}
    )
```
  - Change `parse_message`'s return type to `BookUpdate | TradeBatch | None`, and replace everything after the subscribe-ack handling with:
```python
    channel = msg.get("channel")
    if channel == "book":
        return _parse_book(msg)
    if channel == "trade":
        return _parse_trades(msg)
    return None
```
  - Move the existing book logic, unchanged, into `def _parse_book(msg: dict[str, Any]) -> BookUpdate:`.
  - Add:
```python
def _parse_trades(msg: dict[str, Any]) -> TradeBatch:
    msg_type = msg.get("type")
    if msg_type not in ("snapshot", "update"):
        raise KrakenMessageError(f"unexpected trade message type: {msg_type!r}")
    data = msg.get("data")
    if not isinstance(data, list) or not data:
        raise KrakenMessageError("trade data must be a non-empty list")
    if len(data) > MAX_LEVELS:
        raise KrakenMessageError("too many trades")
    symbols: set[str] = set()
    trades: list[tuple[float, float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise KrakenMessageError("trade must be an object")
        symbol = entry.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise KrakenMessageError("trade missing symbol")
        symbols.add(symbol)
        price = _number(entry.get("price"), "trade price")
        qty = _number(entry.get("qty"), "trade qty")
        if price <= 0 or qty <= 0:
            raise KrakenMessageError("trade out of range")
        trades.append((price, qty))
    if len(symbols) != 1:
        raise KrakenMessageError("trade batch mixes symbols")
    return TradeBatch(symbols.pop(), msg_type == "snapshot", tuple(trades))
```

`python/slipstream/runner.py`: in `on_message`, change `if update is not None:` to `if isinstance(update, BookUpdate):`. Trade forwarding arrives in PR 3.

- [ ] **Step 4: Run the tests (all pass) and the gate. Commit:** `feat(orchestrator): parse Kraken trade channel messages`.

### Task P4: Recorder, calibration-aware replay, `record` CLI

**Files:**
- Create: `python/slipstream/recorder.py`, `python/tests/test_recorder.py`
- Modify: `python/slipstream/replay.py`, `python/slipstream/cli.py`, `python/tests/test_replay.py`, `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

`python/tests/test_recorder.py`:
```python
import asyncio
import json
from pathlib import Path

import pytest
from test_kraken_rest import ohlc_body
from websockets.asyncio.server import ServerConnection, serve

from slipstream.kraken import KrakenMessageError
from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header
from slipstream.replay import read_calibration, read_replay

BOOK = json.dumps({"channel": "book", "type": "snapshot", "data": [
    {"symbol": "BTC/USD", "bids": [{"price": 99.0, "qty": 1.0}],
     "asks": [{"price": 101.0, "qty": 1.0}]}]})
TRADE = json.dumps({"channel": "trade", "type": "update", "data": [
    {"symbol": "BTC/USD", "price": 100.0, "qty": 0.5}]})


def serve_messages(messages: list[str], subscriptions: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    async def handler(ws: ServerConnection) -> None:
        subscriptions.append(json.loads(await ws.recv()))
        subscriptions.append(json.loads(await ws.recv()))
        for message in messages:
            await ws.send(message)
        await ws.wait_closed()

    return serve(handler, "127.0.0.1", 0)


def port_of(server) -> int:  # type: ignore[no-untyped-def]
    return int(next(iter(server.sockets)).getsockname()[1])


def test_open_new_file_refuses_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "s.jsonl"
    existing.write_text("", encoding="utf-8")
    with pytest.raises(RecordError, match="exists"):
        open_new_file(existing)


def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    subscriptions: list[dict[str, object]] = []
    clock = iter(range(1_000, 10_000, 10))

    async def scenario() -> int:
        async with serve_messages([BOOK, TRADE, json.dumps({"channel": "heartbeat"})],
                                  subscriptions) as server:
            with open_new_file(path) as handle:
                write_ohlc_header(handle, 15, ohlc_body())
                write_ohlc_header(handle, 1, ohlc_body())
                return await record_stream(
                    handle, "BTC/USD", 10, duration_s=0.5,
                    url=f"ws://127.0.0.1:{port_of(server)}", clock=lambda: next(clock),
                )

    assert asyncio.run(scenario()) == 3
    channels = sorted(str(s["params"]["channel"]) for s in subscriptions)  # type: ignore[index]
    assert channels == ["book", "trade"]
    records = list(read_replay(path))
    assert [json.loads(raw) for _, raw in records] == [
        json.loads(BOOK), json.loads(TRADE), {"channel": "heartbeat"}]
    assert [ns for ns, _ in records] == [1_000, 1_010, 1_020]
    calibration = read_calibration(path)
    assert sorted(calibration) == [1, 15]
    assert len(calibration[15]) == 2


def test_hostile_stream_message_aborts_recording(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"

    async def scenario() -> int:
        async with serve_messages(["[" * 100_000], []) as server:
            with open_new_file(path) as handle:
                return await record_stream(
                    handle, "BTC/USD", 10, duration_s=2,
                    url=f"ws://127.0.0.1:{port_of(server)}",
                )

    with pytest.raises(KrakenMessageError):
        asyncio.run(scenario())
```

Append to `python/tests/test_replay.py`:
```python
from slipstream.replay import read_calibration
from test_kraken_rest import ohlc_body


def test_replay_skips_ohlc_header_and_calibration_reads_it(tmp_path: Path) -> None:
    ohlc = {"kind": "ohlc", "interval": 15, "data": json.loads(ohlc_body())}
    file = write_lines(tmp_path / "c.jsonl", [
        json.dumps(ohlc), json.dumps({"recv_ns": 5, "msg": {"channel": "heartbeat"}})])
    assert [ns for ns, _ in read_replay(file)] == [5]
    assert len(read_calibration(file)[15]) == 2


@pytest.mark.parametrize(
    "line",
    [
        json.dumps({"kind": "ohlc", "interval": 5, "data": {}}),
        json.dumps({"kind": "ohlc", "interval": 15, "data": {"error": ["x"], "result": {}}}),
        json.dumps({"kind": "ohlc", "interval": True, "data": {}}),
    ],
    ids=["bad-interval", "kraken-error", "bool-interval"],
)
def test_bad_calibration_lines_raise(tmp_path: Path, line: str) -> None:
    with pytest.raises(ReplayError, match="line 1"):
        read_calibration(write_lines(tmp_path / "bad.jsonl", [line]))
```

Append to `python/tests/test_cli.py`:
```python
from pathlib import Path


def test_record_refuses_existing_file_before_any_network(tmp_path: Path) -> None:
    existing = tmp_path / "s.jsonl"
    existing.write_text("keep me", encoding="utf-8")
    assert main(["record", "--duration", "5", "--out", str(existing)]) == 1
    assert existing.read_text(encoding="utf-8") == "keep me"
```
(Merge all new imports into each file's existing import block.)

- [ ] **Step 2: Run the tests and confirm they fail** (`recorder` module / `read_calibration` / `record` subcommand missing).

- [ ] **Step 3: Refactor `python/slipstream/replay.py`**
  - Add a shared record iterator, so the hostile-input handling is written once.
  - Skip OHLC header lines in `read_replay`.
  - Add `read_calibration`.

  The complete new file:
```python
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from slipstream.kraken import KrakenMessageError
from slipstream.kraken_rest import SUPPORTED_INTERVALS, Bar, parse_ohlc
from slipstream.runner import ExecutionRunner


class ReplayError(ValueError):
    pass


def _records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        lineno = 0
        while True:
            lineno += 1
            try:
                line = handle.readline()
            except UnicodeDecodeError as exc:
                raise ReplayError(f"line {lineno}: invalid UTF-8") from exc
            if line == "":
                return
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except (ValueError, RecursionError) as exc:
                raise ReplayError(f"line {lineno}: malformed replay record") from exc
            if not isinstance(record, dict):
                raise ReplayError(f"line {lineno}: malformed replay record")
            yield lineno, record


def _is_ohlc(record: dict[str, Any]) -> bool:
    return record.get("kind") == "ohlc"


def read_replay(path: Path) -> Iterator[tuple[int, str]]:
    for lineno, record in _records(path):
        if _is_ohlc(record):
            continue
        recv_ns = record.get("recv_ns")
        if "msg" not in record:
            raise ReplayError(f"line {lineno}: malformed replay record")
        if isinstance(recv_ns, bool) or not isinstance(recv_ns, int) or recv_ns < 0:
            raise ReplayError(f"line {lineno}: recv_ns must be a non-negative integer")
        yield recv_ns, json.dumps(record["msg"])


def read_calibration(path: Path) -> dict[int, tuple[Bar, ...]]:
    bars: dict[int, tuple[Bar, ...]] = {}
    for lineno, record in _records(path):
        if not _is_ohlc(record):
            continue
        interval = record.get("interval")
        if isinstance(interval, bool) or interval not in SUPPORTED_INTERVALS:
            raise ReplayError(f"line {lineno}: unsupported OHLC interval")
        try:
            bars[interval] = parse_ohlc(json.dumps(record.get("data")))
        except KrakenMessageError as exc:
            raise ReplayError(f"line {lineno}: invalid OHLC data: {exc}") from exc
    return bars


def run_replay(runner: ExecutionRunner, records: Iterable[tuple[int, str]]) -> None:
    last_ns = 0
    for recv_ns, raw in records:
        if recv_ns < last_ns:
            raise ReplayError("replay timestamps must be non-decreasing")
        last_ns = recv_ns
        runner.on_message(raw, recv_ns)
        if runner.is_done():
            return
```
The existing `test_rejects_malformed_records` cases must still fail with `line 1`: `{"msg": {}}` (no `recv_ns`) fails the int check, and `{"recv_ns": 5}` (no `msg`) fails the `msg` check.

- [ ] **Step 4: Implement `python/slipstream/recorder.py`**

```python
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from slipstream.kraken import (
    KRAKEN_WS_URL,
    MAX_MESSAGE_BYTES,
    parse_message,
    subscribe_message,
    subscribe_trades_message,
)
from slipstream.kraken_rest import parse_ohlc

IDLE_TIMEOUT_S = 30.0


class RecordError(RuntimeError):
    pass


def open_new_file(path: Path) -> TextIO:
    try:
        return path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RecordError(f"{path} already exists; refusing to overwrite") from exc


def write_ohlc_header(handle: TextIO, interval_min: int, raw: bytes) -> None:
    parse_ohlc(raw)
    record = {"kind": "ohlc", "interval": interval_min, "data": json.loads(raw)}
    handle.write(json.dumps(record) + "\n")


async def record_stream(
    handle: TextIO,
    symbol: str,
    depth: int,
    duration_s: float,
    url: str = KRAKEN_WS_URL,
    clock: Callable[[], int] = time.time_ns,
) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_s
    written = 0
    try:
        async with connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=10) as ws:
            await ws.send(subscribe_message(symbol, depth))
            await ws.send(subscribe_trades_message(symbol))
            while (remaining := deadline - loop.time()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(IDLE_TIMEOUT_S, remaining))
                except TimeoutError:
                    if loop.time() >= deadline:
                        break
                    raise RecordError("no market data received (idle timeout)") from None
                parse_message(raw)
                handle.write(json.dumps({"recv_ns": clock(), "msg": json.loads(raw)}) + "\n")
                written += 1
    except (WebSocketException, OSError) as exc:
        raise RecordError(f"market data connection failed: {exc}") from exc
    return written
```

- [ ] **Step 5: Wire `record` into `python/slipstream/cli.py`**
  - Add imports: `from slipstream.kraken_rest import fetch_ohlc` and `from slipstream.recorder import RecordError, open_new_file, record_stream, write_ohlc_header`.
  - In `build_parser`, after the `replay` subparser:
```python
    record = commands.add_parser("record", help="record the live Kraken book + trades to JSONL")
    record.add_argument("--duration", type=_positive_int, required=True, help="seconds")
    record.add_argument("--out", type=Path, required=True)
    record.add_argument("--symbol", type=_symbol, default="BTC/USD")
    record.add_argument("--depth", type=int, choices=[10, 25, 100], default=10)
```
  - Add:
```python
def _record(args: argparse.Namespace, log: logging.Logger) -> int:
    try:
        with open_new_file(args.out) as handle:
            for interval in (15, 1):
                write_ohlc_header(handle, interval, fetch_ohlc(args.symbol, interval))
            count = asyncio.run(record_stream(handle, args.symbol, args.depth, args.duration))
    except (RecordError, KrakenMessageError, OSError) as exc:
        log.error(str(exc))
        return 1
    log.info("recording complete", extra={"fields": {"event": "record", "messages": count,
                                                     "path": str(args.out)}})
    return 0
```
  - In `main`, directly after `log = configure_logging()`, add `if args.command == "record": return _record(args, log)`. Also add `import logging`.

- [ ] **Step 6: Run the full gate** (`bash scripts/ci.sh` → `CI OK`, which includes the existing end-to-end test). Commit: `feat(orchestrator): record live sessions with calibration data for exact replay`.

- [ ] **Step 7: Track P finish.** Run `/security-review` (new outbound HTTPS, file writes), then receiving-code-review, then verification. Push the branch, open PR 2, and merge once CI is green.

---

## After PRs 1 and 2 merge

Write `docs/superpowers/plans/<date>-execution-schedules-integration.md` for PR 3:
- `EngineClient.apply_trades` and schedule parameters on `submit`.
- `ExecutionRunner` with a list of specs, calibration at the first snapshot, and trade forwarding.
- The `--algo`/`--urgency`/`--risk-aversion`/`--participation` flags.
- `compare`.
- A four-algorithm end-to-end fixture.
- The README with a real comparison run.
