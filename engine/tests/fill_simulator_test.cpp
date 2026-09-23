#include "fill_simulator.h"

#include <gtest/gtest.h>

using slipstream::simulate_market_fill;

TEST(FillSimulator, FillsWithinFirstLevel) {
    const auto result = simulate_market_fill({{101.0, 1.0}, {102.0, 3.0}}, 0.5);
    EXPECT_DOUBLE_EQ(result.filled_qty, 0.5);
    EXPECT_DOUBLE_EQ(result.avg_price, 101.0);
}

TEST(FillSimulator, WalksMultipleLevelsForVolumeWeightedPrice) {
    const auto result = simulate_market_fill({{101.0, 1.0}, {102.0, 3.0}}, 2.0);
    EXPECT_DOUBLE_EQ(result.filled_qty, 2.0);
    EXPECT_DOUBLE_EQ(result.avg_price, 101.5);
}

TEST(FillSimulator, PartialFillWhenBookTooThin) {
    const auto result = simulate_market_fill({{101.0, 1.0}}, 3.0);
    EXPECT_DOUBLE_EQ(result.filled_qty, 1.0);
    EXPECT_DOUBLE_EQ(result.avg_price, 101.0);
}

TEST(FillSimulator, NothingFilledOnEmptyBookOrZeroQty) {
    EXPECT_DOUBLE_EQ(simulate_market_fill({}, 1.0).filled_qty, 0.0);
    EXPECT_DOUBLE_EQ(simulate_market_fill({{101.0, 1.0}}, 0.0).filled_qty, 0.0);
}
