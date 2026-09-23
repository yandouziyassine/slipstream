#include "twap.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <limits>

using slipstream::TwapSchedule;

TEST(Twap, RejectsInvalidParams) {
    EXPECT_FALSE(TwapSchedule::create({0.0, 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({-1.0, 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({std::nan(""), 0, 60, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 0, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 60, 0}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 60, TwapSchedule::kMaxSlices + 1}));
    EXPECT_FALSE(TwapSchedule::create({1.0, 0, 5, 6}));
    EXPECT_FALSE(TwapSchedule::create({1.0, -1, 60, 6}));
}

TEST(Twap, NothingReleasedBeforeStart) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(999), 0.0);
}

TEST(Twap, FirstSliceReleasedAtStart) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1000), 0.2);
}

TEST(Twap, OneSliceReleasedPerInterval) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1099), 0.2);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1100), 0.4);
    EXPECT_DOUBLE_EQ(schedule->target_qty_at(1250), 0.6);
}

TEST(Twap, ReleasesExactTotalAfterLastSlice) {
    const auto schedule = TwapSchedule::create({1.2, 1000, 600, 6});
    ASSERT_TRUE(schedule);
    EXPECT_EQ(schedule->target_qty_at(1500), 1.2);
    EXPECT_EQ(schedule->target_qty_at(10'000'000), 1.2);
}

TEST(Twap, ExtremeTimestampsDoNotOverflow) {
    const auto schedule = TwapSchedule::create({1.0, 0, 1, 1});
    ASSERT_TRUE(schedule);
    EXPECT_EQ(schedule->target_qty_at(std::numeric_limits<std::int64_t>::max()), 1.0);
    const auto many = TwapSchedule::create({2.0, 0, 1000, 1000});
    ASSERT_TRUE(many);
    EXPECT_EQ(many->target_qty_at(std::numeric_limits<std::int64_t>::max()), 2.0);
    EXPECT_DOUBLE_EQ(many->target_qty_at(std::numeric_limits<std::int64_t>::min()), 0.0);
}
