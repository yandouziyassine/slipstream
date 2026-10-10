#include <gtest/gtest.h>

#include <cstdint>
#include <string>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;
constexpr const char* kReason = "no fresh market data for 30s";

// Two fee-free venues that go stale 2 s after their last book change. Orders run 100 s in
// 4 slices, so the first slice fills on submit and the deadline is far beyond the tests.
class MarketHaltTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  2 * kSec};

    void SetUp() override {
        refresh(0, kSec);
        refresh(1, kSec);
    }

    void refresh(std::size_t venue, std::int64_t at_ns) {
        ASSERT_TRUE(engine.apply_book_snapshot(venue, {{99.0, 50.0}}, {{100.0, 50.0}}, at_ns));
    }

    void submit(const std::string& id, std::int64_t at_ns = kSec) {
        ASSERT_TRUE(engine.submit({id, Side::Buy, 1.0, at_ns, 100 * kSec, 4}).accepted);
    }

    OrderState state(std::size_t order = 0) { return engine.statuses().at(order).state; }
};

}  // namespace

TEST_F(MarketHaltTest, HaltsEveryWorkingOrderAfter30sWithoutAUsableMarket) {
    submit("a");
    submit("b");
    ASSERT_EQ(engine.step(kSec).fills.size(), 2u);

    const auto out = engine.step(32 * kSec);
    EXPECT_TRUE(out.fills.empty());
    ASSERT_EQ(out.updates.size(), 2u);
    for (const auto& update : out.updates) {
        EXPECT_EQ(update.state, OrderState::Halted);
        EXPECT_EQ(update.reason, kReason);
        EXPECT_DOUBLE_EQ(update.filled_qty, 0.25);
    }
    for (const auto& status : engine.statuses()) {
        EXPECT_EQ(status.state, OrderState::Halted);
        EXPECT_EQ(status.halt_reason, kReason);
    }
    EXPECT_TRUE(engine.step(40 * kSec).updates.empty());
}

TEST_F(MarketHaltTest, KeepsWorkingAfter29sWithoutAUsableMarket) {
    submit("o");
    engine.step(kSec);
    EXPECT_TRUE(engine.step(30 * kSec).updates.empty());
    EXPECT_EQ(state(), OrderState::Working);
}

TEST_F(MarketHaltTest, ExactlyTheLimitIsNotYetAHalt) {
    submit("o");
    engine.step(kSec);
    EXPECT_TRUE(engine.step(kSec + Engine::kNoMarketHaltNs).updates.empty());
    EXPECT_EQ(state(), OrderState::Working);
    engine.step(kSec + Engine::kNoMarketHaltNs + 1);
    EXPECT_EQ(state(), OrderState::Halted);
}

TEST_F(MarketHaltTest, OneFreshVenueKeepsOrdersWorking) {
    submit("o");
    engine.step(kSec);
    for (std::int64_t t = 15; t <= 45; t += 15) {
        refresh(1, t * kSec);
        engine.step(t * kSec);
    }
    EXPECT_EQ(state(), OrderState::Working);
}

TEST_F(MarketHaltTest, VenueEmptiedBySnapshotIsNotAUsableMarket) {
    submit("o");
    engine.step(kSec);
    for (std::int64_t t = 15; t <= 30; t += 15) {
        ASSERT_TRUE(engine.apply_book_snapshot(1, {}, {}, t * kSec));
        engine.step(t * kSec);
    }
    ASSERT_TRUE(engine.apply_book_snapshot(1, {}, {}, 32 * kSec));
    engine.step(32 * kSec);
    EXPECT_EQ(state(), OrderState::Halted);
    EXPECT_EQ(engine.statuses().at(0).halt_reason, kReason);
}

TEST_F(MarketHaltTest, MarketReturningAt29sRestartsTheTimer) {
    submit("o");
    engine.step(kSec);
    refresh(0, 30 * kSec);
    engine.step(30 * kSec);
    engine.step(33 * kSec);  // stale again, and 32 s after the first market
    engine.step(60 * kSec);
    EXPECT_EQ(state(), OrderState::Working);
    engine.step(61 * kSec);
    EXPECT_EQ(state(), OrderState::Halted);
}

TEST_F(MarketHaltTest, TimerStartsWhenAnOrderIsAccepted) {
    engine.step(kSec);
    refresh(0, 50 * kSec);
    submit("o", 50 * kSec);
    engine.step(79 * kSec);
    EXPECT_EQ(state(), OrderState::Working);
    engine.step(81 * kSec);
    EXPECT_EQ(state(), OrderState::Halted);
}

TEST_F(MarketHaltTest, HaltReleasesConsumptionOverlays) {
    submit("o");
    engine.step(kSec);
    ASSERT_GT(engine.consumption_entries(), 0u);
    engine.step(32 * kSec);
    ASSERT_EQ(state(), OrderState::Halted);
    EXPECT_EQ(engine.consumption_entries(), 0u);
}

TEST_F(MarketHaltTest, FinishedOrdersAreNotReportedAgain) {
    ASSERT_TRUE(engine.submit({"done", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    submit("o");
    engine.step(kSec);
    ASSERT_EQ(state(0), OrderState::Completed);

    const auto out = engine.step(32 * kSec);
    ASSERT_EQ(out.updates.size(), 1u);
    EXPECT_EQ(out.updates[0].order_id, "o");
    EXPECT_EQ(state(0), OrderState::Completed);
}

TEST(MarketHaltSingleVenue, EmptyBookHaltsTheLegacyEngine) {
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 50.0}}, {{100.0, 50.0}}));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 0, 100 * kSec, 4}).accepted);
    engine.step(0);
    ASSERT_TRUE(engine.apply_book_snapshot({}, {}));
    engine.step(30 * kSec);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Working);
    engine.step(31 * kSec);
    EXPECT_EQ(engine.statuses().at(0).state, OrderState::Halted);
}
