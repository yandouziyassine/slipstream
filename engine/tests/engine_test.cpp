#include "engine.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

class EngineTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}, {98.0, 5.0}},
                                               {{101.0, 1.0}, {102.0, 10.0}}));
    }

    static ParentOrderRequest buy(double qty, std::int32_t slices) {
        return {"order-1", Side::Buy, qty, 0, 4 * kSec, slices};
    }
};

}  // namespace

TEST(EngineNoData, SubmitRejectedWithoutMarketData) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    const auto result = engine.submit({"order-1", Side::Buy, 1.0, 0, kSec, 1});
    EXPECT_FALSE(result.accepted);
    EXPECT_EQ(result.reason, "no market data");
}

TEST_F(EngineTest, SubmitRecordsArrivalMidAndImmediateCost) {
    ASSERT_TRUE(engine.submit(buy(2.0, 4)).accepted);
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Working);
    EXPECT_DOUBLE_EQ(status.arrival_mid, 100.0);
    EXPECT_DOUBLE_EQ(status.immediate_cost_bps, 150.0);
}

TEST_F(EngineTest, RejectsDuplicateAndInvalidOrderIds) {
    ASSERT_TRUE(engine.submit(buy(0.1, 1)).accepted);
    EXPECT_EQ(engine.submit(buy(0.1, 1)).reason, "duplicate order id");
    for (const auto& id : {std::string(""), std::string("bad id!"), std::string(65, 'a'),
                           std::string("caf\xC3\xA9"), std::string("id\xB2")}) {
        auto request = buy(0.1, 1);
        request.order_id = id;
        EXPECT_EQ(engine.submit(request).reason, "invalid order id");
    }
}

TEST_F(EngineTest, RejectsInvalidSchedule) {
    EXPECT_EQ(engine.submit(buy(1.0, 0)).reason, "invalid schedule");
}

TEST_F(EngineTest, ProjectedPositionCountsWorkingOrders) {
    Engine limited(RiskLimits{1'000'000.0, 3.0}, 10);
    ASSERT_TRUE(limited.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(limited.submit({"a", Side::Buy, 2.0, 0, kSec, 1}).accepted);
    EXPECT_EQ(limited.submit({"b", Side::Buy, 2.0, 0, kSec, 1}).reason, "position limit exceeded");
}

TEST_F(EngineTest, RejectsWhenOrderCapacityReached) {
    for (std::size_t i = 0; i < Engine::kMaxOrders; ++i) {
        ASSERT_TRUE(engine.submit({"o" + std::to_string(i), Side::Buy, 0.001, 0, kSec, 1}).accepted);
    }
    EXPECT_EQ(engine.submit({"overflow", Side::Buy, 0.001, 0, kSec, 1}).reason,
              "order capacity reached");
}

TEST_F(EngineTest, StepReleasesSlicesOverTime) {
    ASSERT_TRUE(engine.submit(buy(2.0, 4)).accepted);

    auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[0].price, 101.0);

    EXPECT_TRUE(engine.step(0).fills.empty());
    ASSERT_EQ(engine.step(kSec).fills.size(), 1u);

    fills = engine.step(3 * kSec).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 1.0);

    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Completed);
    EXPECT_DOUBLE_EQ(status.filled_qty, 2.0);
    EXPECT_DOUBLE_EQ(status.avg_fill_price, 101.0);
    EXPECT_DOUBLE_EQ(status.slippage_bps, 100.0);
    EXPECT_DOUBLE_EQ(engine.position(), 2.0);
}

TEST_F(EngineTest, SellFillsAgainstBids) {
    ASSERT_TRUE(engine.submit({"sell-1", Side::Sell, 1.0, 0, kSec, 1}).accepted);
    const auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].price, 99.0);
    EXPECT_DOUBLE_EQ(engine.position(), -1.0);
    EXPECT_DOUBLE_EQ(engine.statuses().at(0).slippage_bps, 100.0);
}

TEST_F(EngineTest, PartialFillRollsIntoNextStep) {
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 1.0}}));
    ASSERT_TRUE(engine.submit({"thin", Side::Buy, 3.0, 0, kSec, 1}).accepted);
    EXPECT_DOUBLE_EQ(engine.step(0).fills.at(0).qty, 1.0);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Working);
    EXPECT_DOUBLE_EQ(engine.step(1).fills.at(0).qty, 1.0);
    EXPECT_DOUBLE_EQ(engine.step(2).fills.at(0).qty, 1.0);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Completed);
}

TEST(EngineRisk, RoutedBookWalkCostIsCheckedNotTheMidEstimate) {
    Engine engine(RiskLimits{1000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{98.0, 20.0}}, {{100.0, 20.0}}));
    ASSERT_TRUE(
        engine.submit({"o", Side::Buy, 9.98, 0, 2 * kSec, 2}, VwapSpec{{9.9, 0.08}}).accepted);
    ASSERT_EQ(engine.step(0).fills.size(), 1u);  // 9.9 at 100 spends 990

    // Mid 100: the 0.08 child looks like 8 at mid, but walking the book costs 4.004 + 7.996 = 12.
    ASSERT_TRUE(engine.apply_book_snapshot({{99.9, 5.0}}, {{100.1, 0.04}, {199.9, 5.0}}));
    EXPECT_TRUE(engine.step(kSec).fills.empty());

    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "order notional limit exceeded");
    EXPECT_NEAR(status.filled_qty, 9.9, 1e-12);
    EXPECT_NEAR(engine.position(), 9.9, 1e-12);
}

TEST(EngineRisk, FeesCountTowardTheNotionalLimit) {
    Engine engine(RiskLimits{1000.0, 100.0}, 10, {{"kraken", 100.0}}, 0);
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{98.0, 20.0}}, {{100.0, 20.0}}, 0));
    // Mid 99 x 9.95 = 985 passes the parent check; the route costs 995 + 9.95 fee = 1004.95.
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 9.95, 0, kSec, 1}).accepted);
    EXPECT_TRUE(engine.step(0).fills.empty());
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "order notional limit exceeded");
    EXPECT_DOUBLE_EQ(engine.position(), 0.0);
}

TEST(EngineCollar, BuyUsesLevelsWithinTheCollarOnly) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}}, 0, 50.0);
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.9, 5.0}},
                                           {{100.1, 0.1}, {100.4, 0.1}, {100.6, 5.0}}, 0));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 0, 10 * kSec, 1}).accepted);
    EXPECT_NEAR(engine.statuses().at(0).immediate_cost_bps, 25.0, 1e-9);
    const auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_NEAR(fills[0].qty, 0.2, 1e-12);
    EXPECT_NEAR(fills[0].price, 100.25, 1e-9);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Working);
}

TEST(EngineCollar, SellUsesLevelsWithinTheCollarOnly) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}}, 0, 50.0);
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.9, 0.1}, {99.6, 0.1}, {99.4, 5.0}},
                                           {{100.1, 5.0}}, 0));
    ASSERT_TRUE(engine.submit({"o", Side::Sell, 1.0, 0, 10 * kSec, 1}).accepted);
    EXPECT_NEAR(engine.statuses().at(0).immediate_cost_bps, 25.0, 1e-9);
    const auto fills = engine.step(0).fills;
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_NEAR(fills[0].qty, 0.2, 1e-12);
    EXPECT_NEAR(fills[0].price, 99.75, 1e-9);
}

TEST(EngineCollar, CounterfactualVenueRouteUsesTheCollar) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}}, kSec,
                  50.0);
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.9, 5.0}}, {{100.1, 1.0}}, 0));
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.9, 5.0}}, {{100.2, 0.1}, {100.8, 5.0}}, 0));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 0.5, 0, 10 * kSec, 1}).accepted);
    ASSERT_EQ(engine.step(0).fills.size(), 1u);
    const auto status = engine.statuses().at(0);
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_FALSE(status.venue_costs[1].available);
}

TEST(EngineRisk, PriceSpikeHaltsOrderOnNotionalBudget) {
    Engine engine(RiskLimits{250.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"order-1", Side::Buy, 2.0, 0, 2 * kSec, 2}).accepted);
    ASSERT_EQ(engine.step(0).fills.size(), 1u);

    ASSERT_TRUE(engine.apply_book_snapshot({{199.0, 5.0}}, {{201.0, 5.0}}));
    EXPECT_TRUE(engine.step(kSec).fills.empty());

    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "order notional limit exceeded");
    EXPECT_DOUBLE_EQ(engine.position(), 1.0);
}

TEST_F(EngineTest, StepReportsAnUpdateForEachFillAndTheFinalState) {
    ASSERT_TRUE(engine.submit(buy(2.0, 4)).accepted);

    auto out = engine.step(0);
    ASSERT_EQ(out.fills.size(), 1u);
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].order_id, "order-1");
    EXPECT_EQ(out.updates[0].state, OrderState::Working);
    EXPECT_EQ(out.updates[0].reason, "");
    EXPECT_DOUBLE_EQ(out.updates[0].filled_qty, 0.5);

    out = engine.step(0);
    EXPECT_TRUE(out.fills.empty());
    EXPECT_TRUE(out.updates.empty());

    out = engine.step(3 * kSec);
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].state, OrderState::Completed);
    EXPECT_DOUBLE_EQ(out.updates[0].filled_qty, 2.0);

    EXPECT_TRUE(engine.step(4 * kSec).updates.empty());
}

TEST(EngineUpdates, RiskHaltIsReportedWithItsReason) {
    Engine engine(RiskLimits{250.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"order-1", Side::Buy, 2.0, 0, 2 * kSec, 2}).accepted);
    ASSERT_EQ(engine.step(0).updates.size(), 1u);

    ASSERT_TRUE(engine.apply_book_snapshot({{199.0, 5.0}}, {{201.0, 5.0}}));
    const auto out = engine.step(kSec);
    EXPECT_TRUE(out.fills.empty());
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].state, OrderState::Halted);
    EXPECT_EQ(out.updates[0].reason, "order notional limit exceeded");
    EXPECT_DOUBLE_EQ(out.updates[0].filled_qty, 1.0);
}

TEST(EngineUpdates, DeadlineHaltIsReported) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 1.0}}));
    ASSERT_TRUE(engine.submit({"thin", Side::Buy, 3.0, 0, kSec, 1}).accepted);
    ASSERT_EQ(engine.step(0).updates.size(), 1u);

    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {}));
    const auto out = engine.step(2 * kSec);
    EXPECT_TRUE(out.fills.empty());
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].order_id, "thin");
    EXPECT_EQ(out.updates[0].state, OrderState::Halted);
    EXPECT_EQ(out.updates[0].reason, "deadline reached");
    EXPECT_DOUBLE_EQ(out.updates[0].filled_qty, 1.0);
}

TEST(EngineUpdates, OnlyOrdersThatChangedAreReported) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"now", Side::Buy, 1.0, 0, kSec, 1}).accepted);
    ASSERT_TRUE(engine.submit({"later", Side::Buy, 1.0, 5 * kSec, kSec, 1}).accepted);
    const auto out = engine.step(0);
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].order_id, "now");
    EXPECT_EQ(out.updates[0].state, OrderState::Completed);
    EXPECT_TRUE(engine.step(kSec).updates.empty());
}
