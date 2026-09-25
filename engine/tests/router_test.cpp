#include "router.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
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

TEST(Router, NonPositiveOrNanQuantityFillsNothing) {
    const std::vector<VenueLiquidity> venues{{0, 0.0, {{100.0, 1.0}}}};
    for (const double qty : {0.0, -1.0, std::nan("")}) {
        const auto result = route(Side::Buy, qty, venues);
        EXPECT_DOUBLE_EQ(result.filled_qty, 0.0) << qty;
        EXPECT_TRUE(result.legs.empty()) << qty;
    }
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
