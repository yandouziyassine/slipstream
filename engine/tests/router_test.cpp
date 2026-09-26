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

TEST(Router, QtyStepFloorsLegAndTrimsTheWorstPrice) {
    VenueLiquidity venue{0, 0.001, {{100.0, 0.02}, {101.0, 0.05}}};
    venue.qty_step = 0.01;
    const auto result = route(Side::Buy, 0.037, {venue});
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_NEAR(result.legs[0].qty, 0.03, 1e-12);
    EXPECT_NEAR(result.legs[0].gross_notional, 0.02 * 100.0 + 0.01 * 101.0, 1e-9);
    EXPECT_NEAR(result.legs[0].fee, (0.02 * 100.0 + 0.01 * 101.0) * 0.001, 1e-12);
    EXPECT_NEAR(result.filled_qty, 0.03, 1e-12);
    EXPECT_NEAR(result.gross_notional, 3.01, 1e-9);
    EXPECT_NEAR(result.fees, 0.00301, 1e-12);
}

TEST(Router, QtyStepFloorsToZeroDropsTheLeg) {
    VenueLiquidity venue{0, 0.0, {{100.0, 1.0}}};
    venue.qty_step = 0.01;
    const auto result = route(Side::Buy, 0.004, {venue});
    EXPECT_TRUE(result.legs.empty());
    EXPECT_DOUBLE_EQ(result.filled_qty, 0.0);
    EXPECT_DOUBLE_EQ(result.gross_notional, 0.0);
}

TEST(Router, ExactStepMultipleIsNotFlooredAway) {
    VenueLiquidity venue{0, 0.0, {{100.0, 1.0}}};
    venue.qty_step = 0.00000001;
    venue.min_qty = 0.00005;
    const auto result = route(Side::Buy, 0.00005, {venue});
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_NEAR(result.legs[0].qty, 0.00005, 1e-15);
}

TEST(Router, LegBelowMinQtyIsDroppedAndOtherLegSurvives) {
    VenueLiquidity thin{1, 0.0, {{100.1, 1.0}}};
    thin.min_qty = 0.6;
    const auto result = route(Side::Buy, 1.0, {{0, 0.0, {{100.0, 0.5}}}, thin});
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_EQ(result.legs[0].venue, 0u);
    EXPECT_DOUBLE_EQ(result.filled_qty, 0.5);
    EXPECT_DOUBLE_EQ(result.gross_notional, 50.0);
    EXPECT_DOUBLE_EQ(result.fees, 0.0);
}

TEST(Router, LegBelowMinNotionalIsDropped) {
    VenueLiquidity small{1, 0.01, {{100.1, 1.0}}};
    small.min_notional = 60.0;
    const auto result = route(Side::Buy, 1.0, {{0, 0.0, {{100.0, 0.5}}}, small});
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_EQ(result.legs[0].venue, 0u);
    EXPECT_DOUBLE_EQ(result.filled_qty, 0.5);
    EXPECT_DOUBLE_EQ(result.fees, 0.0);

    small.min_notional = 50.0;
    EXPECT_EQ(route(Side::Buy, 1.0, {{0, 0.0, {{100.0, 0.5}}}, small}).legs.size(), 2u);
}

TEST(Router, SellLegsAreFlooredAndCheckedToo) {
    VenueLiquidity venue{0, 0.0, {{100.0, 0.02}, {99.0, 0.05}}};
    venue.qty_step = 0.01;
    venue.min_notional = 2.5;
    const auto result = route(Side::Sell, 0.037, {venue});
    ASSERT_EQ(result.legs.size(), 1u);
    EXPECT_NEAR(result.gross_notional, 0.02 * 100.0 + 0.01 * 99.0, 1e-9);
    EXPECT_TRUE(route(Side::Sell, 0.024, {venue}).legs.empty());
}

TEST(Router, ZeroRulesReproduceTheUnconstrainedWalk) {
    const std::vector<VenueLiquidity> plain{{0, 0.004, {{100.0, 1.0}}},
                                            {1, 0.0, {{100.3, 0.5}, {100.6, 5.0}}}};
    const std::vector<VenueLiquidity> zero_rules{
        {0, 0.004, {{100.0, 1.0}}, 0.0, 0.0, 0.0},
        {1, 0.0, {{100.3, 0.5}, {100.6, 5.0}}, 0.0, 0.0, 0.0}};
    const auto a = route(Side::Buy, 1.2, plain);
    const auto b = route(Side::Buy, 1.2, zero_rules);
    ASSERT_EQ(a.legs.size(), b.legs.size());
    for (std::size_t i = 0; i < a.legs.size(); ++i) {
        EXPECT_EQ(a.legs[i].venue, b.legs[i].venue);
        EXPECT_EQ(a.legs[i].qty, b.legs[i].qty);
        EXPECT_EQ(a.legs[i].gross_notional, b.legs[i].gross_notional);
        EXPECT_EQ(a.legs[i].fee, b.legs[i].fee);
    }
    EXPECT_DOUBLE_EQ(a.filled_qty, 1.2);
    EXPECT_DOUBLE_EQ(a.gross_notional, 0.5 * 100.3 + 0.7 * 100.0);
    EXPECT_DOUBLE_EQ(a.fees, 0.7 * 100.0 * 0.004);
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
