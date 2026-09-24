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
