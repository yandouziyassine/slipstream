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
