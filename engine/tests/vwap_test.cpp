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
