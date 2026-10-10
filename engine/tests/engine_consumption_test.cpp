#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

double bps(double avg, double ref) { return (avg - ref) / ref * 1e4; }

// One fee-free venue. Two TWAP children of 0.8 each: the first takes 0.8 of the 1.0 at 100,
// so without a feed change the second finds 0.2 at 100 and pays 101 for the other 0.6.
class ConsumptionTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{100.0, 1.0}, {101.0, 5.0}}));
    }

    void submit(const std::string& id) {
        ASSERT_TRUE(engine.submit({id, Side::Buy, 1.6, 0, 2 * kSec, 2}).accepted);
    }
};

constexpr double kConsumedAvg = (0.2 * 100.0 + 0.6 * 101.0) / 0.8;

}  // namespace

TEST_F(ConsumptionTest, SecondChildSeesWhatTheFirstLeft) {
    submit("o");
    auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.8);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
    fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.8);
    EXPECT_DOUBLE_EQ(fills[0].price, kConsumedAvg);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Completed);
}

TEST_F(ConsumptionTest, DeltaAtTheConsumedPriceResetsIt) {
    submit("o");
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    ASSERT_TRUE(engine.apply_book_update({}, {{100.0, 1.0}}));
    const auto fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
}

TEST_F(ConsumptionTest, DeltaAtAnotherPriceKeepsTheConsumption) {
    submit("o");
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    ASSERT_TRUE(engine.apply_book_update({{99.0, 4.0}}, {{101.0, 5.0}}));
    const auto fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].price, kConsumedAvg);
}

TEST_F(ConsumptionTest, SnapshotResetsEverything) {
    submit("o");
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{100.0, 1.0}, {101.0, 5.0}}));
    const auto fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
}

TEST_F(ConsumptionTest, OrdersDoNotConsumeEachOthersLiquidity) {
    submit("a");
    submit("b");
    auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
    EXPECT_DOUBLE_EQ(fills[1].price, 100.0);
    fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_DOUBLE_EQ(fills[0].price, kConsumedAvg);
    EXPECT_DOUBLE_EQ(fills[1].price, kConsumedAvg);
}

TEST_F(ConsumptionTest, SingleVenueRoutedCostEqualsItsVenueAloneCost) {
    submit("o");
    engine.step(0);
    engine.step(kSec);
    const auto status = engine.statuses().at(0);
    const double avg = (0.8 * 100.0 + 0.8 * kConsumedAvg) / 1.6;
    EXPECT_NEAR(status.routed_all_in_bps, bps(avg, 99.5), 1e-9);
    ASSERT_EQ(status.venue_costs.size(), 1u);
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_NEAR(status.venue_costs[0].all_in_bps, status.routed_all_in_bps, 1e-9);
}

TEST_F(ConsumptionTest, CompletionReleasesTheOrdersRecords) {
    submit("o");
    engine.step(0);
    EXPECT_GT(engine.consumption_entries(), 0u);
    engine.step(kSec);
    ASSERT_EQ(engine.statuses().at(0).state, OrderState::Completed);
    EXPECT_EQ(engine.consumption_entries(), 0u);
}

TEST_F(ConsumptionTest, DeadlineHaltReleasesTheOrdersRecords) {
    submit("o");
    engine.step(0);
    EXPECT_GT(engine.consumption_entries(), 0u);
    ASSERT_TRUE(engine.apply_book_update({}, {{100.0, 0.0}, {101.0, 0.0}}));
    engine.step(10 * kSec);
    const auto status = engine.statuses().at(0);
    ASSERT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "deadline reached");
    EXPECT_EQ(engine.consumption_entries(), 0u);
}

TEST(Consumption, RiskLimitSeesTheCostAfterConsumption) {
    // Parent notional 1.4 x 99.5 = 139.3 fits 160. Without consumption the children would cost
    // 70 + 70; with it the second pays 150 for 0.7, so 70 + 105 breaches the limit and halts.
    Engine engine(RiskLimits{160.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{100.0, 0.7}, {150.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.4, 0, 2 * kSec, 2}).accepted);
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    EXPECT_TRUE(engine.step(kSec).fills.empty());
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "order notional limit exceeded");
    EXPECT_DOUBLE_EQ(engine.position(), 0.7);
    EXPECT_EQ(engine.consumption_entries(), 0u);
}

TEST(Consumption, SellsConsumeBids) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{100.0, 1.0}, {99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"s", Side::Sell, 1.6, 0, 2 * kSec, 2}).accepted);
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    const auto fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].price, (0.2 * 100.0 + 0.6 * 99.0) / 0.8);
}

namespace {

// Two fee-free venues. Kraken's best ask is cheaper, so the first child takes it; the second
// child finds only 0.2 left there and takes the rest from Coinbase's best ask.
class MultiVenueConsumptionTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  100 * kSec};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}, {102.0, 5.0}}, 0));
        ASSERT_TRUE(
            engine.apply_book_snapshot(1, {{99.0, 5.0}}, {{100.5, 1.0}, {103.0, 5.0}}, 0));
        ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.6, 0, 2 * kSec, 2}).accepted);
    }
};

}  // namespace

TEST_F(MultiVenueConsumptionTest, SecondChildMovesToTheOtherVenue) {
    auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.8);
    fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_NEAR(fills[0].qty, 0.2, 1e-12);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_NEAR(fills[1].qty, 0.6, 1e-12);
    EXPECT_DOUBLE_EQ(fills[1].price, 100.5);
}

TEST_F(MultiVenueConsumptionTest, EachVenueAloneCostPaysForItsOwnConsumption) {
    engine.step(0);
    engine.step(kSec);
    const auto status = engine.statuses().at(0);
    const double arrival = (99.0 + 100.0) / 2.0;
    EXPECT_NEAR(status.routed_all_in_bps, bps((80.0 + 20.0 + 0.6 * 100.5) / 1.6, arrival), 1e-9);
    ASSERT_EQ(status.venue_costs.size(), 2u);
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_NEAR(status.venue_costs[0].all_in_bps, bps((80.0 + 20.0 + 0.6 * 102.0) / 1.6, arrival),
                1e-9);
    EXPECT_TRUE(status.venue_costs[1].available);
    EXPECT_NEAR(status.venue_costs[1].all_in_bps,
                bps((0.8 * 100.5 + 0.2 * 100.5 + 0.6 * 103.0) / 1.6, arrival), 1e-9);
    EXPECT_LT(status.routed_all_in_bps, status.venue_costs[0].all_in_bps);
}

TEST_F(MultiVenueConsumptionTest, ARefreshOnOneVenueLeavesTheOtherConsumed) {
    engine.step(0);
    ASSERT_TRUE(engine.apply_book_update(1, {}, {{100.5, 2.0}}, 0));
    const auto fills = engine.step(kSec).fills;
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_NEAR(fills[0].qty, 0.2, 1e-12);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_NEAR(fills[1].qty, 0.6, 1e-12);
}

TEST(Consumption, VenueAloneThatRunsOutOfItsOwnLiquidityIsUnavailable) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  100 * kSec);
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}}, 0));
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.0, 5.0}}, {{100.5, 5.0}}, 0));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.6, 0, 2 * kSec, 2}).accepted);
    engine.step(0);
    EXPECT_TRUE(engine.statuses().at(0).venue_costs[0].available);
    engine.step(kSec);
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Completed);
    EXPECT_FALSE(status.venue_costs[0].available);
    EXPECT_TRUE(status.venue_costs[1].available);
}

TEST(Consumption, RecordsStayBoundedAcrossManyRefreshes) {
    constexpr int kSteps = 100;
    constexpr std::size_t kLevels = 5;
    Engine engine(RiskLimits{1'000'000.0, 1000.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 50.0}}, {{100.0, 50.0}}));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 10.0, 0, kSteps * kSec, kSteps}).accepted);
    for (int step = 0; step < kSteps; ++step) {
        const double shift = 0.01 * step;
        std::vector<Level> asks;
        for (std::size_t i = 0; i < kLevels; ++i) asks.push_back({100.0 + shift + static_cast<double>(i), 0.05});
        asks.push_back({110.0 + shift, 50.0});
        ASSERT_TRUE(engine.apply_book_snapshot({{99.0 + shift, 50.0}}, asks));
        ASSERT_FALSE(engine.step(step * kSec).fills.empty());
        // One record for the routed order and one for the single-venue counterfactual.
        EXPECT_LE(engine.consumption_entries(), 2 * (kLevels + 1));
    }
}
