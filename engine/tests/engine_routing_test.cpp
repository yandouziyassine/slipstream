#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

double bps(double avg, double ref) { return (avg - ref) / ref * 1e4; }

class RoutingTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10,
                  {{"kraken", 40.0}, {"coinbase", 0.0}}, 2 * kSec};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}}, kSec));
        ASSERT_TRUE(
            engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, kSec));
    }
};

}  // namespace

TEST_F(RoutingTest, SplitsChildAcrossVenuesByAllInPrice) {
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[0].price, 100.0);
    EXPECT_DOUBLE_EQ(fills[0].fee, 0.2);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[1].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[1].price, 100.3);
    EXPECT_DOUBLE_EQ(fills[1].fee, 0.0);

    const auto status = engine.statuses().at(0);
    const double arrival = (99.5 + 100.0) / 2.0;
    EXPECT_DOUBLE_EQ(status.arrival_mid, arrival);
    EXPECT_DOUBLE_EQ(status.fees_paid, 0.2);
    EXPECT_NEAR(status.routed_all_in_bps, bps(100.35, arrival), 1e-9);
    ASSERT_EQ(status.venue_costs.size(), 2u);
    EXPECT_EQ(status.venue_costs[0].venue, "kraken");
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_NEAR(status.venue_costs[0].all_in_bps, bps(100.4, arrival), 1e-9);
    EXPECT_EQ(status.venue_costs[1].venue, "coinbase");
    EXPECT_TRUE(status.venue_costs[1].available);
    EXPECT_NEAR(status.venue_costs[1].all_in_bps, bps(100.45, arrival), 1e-9);
}

TEST_F(RoutingTest, VenueTooThinForTheWholeChildIsUnavailable) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}}, kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    ASSERT_EQ(engine.step(kSec).size(), 2u);
    const auto status = engine.statuses().at(0);
    EXPECT_TRUE(status.venue_costs[0].available);
    EXPECT_FALSE(status.venue_costs[1].available);
}

TEST_F(RoutingTest, StaleVenueIsExcludedFromRoutingAndMid) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, 4 * kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 4 * kSec, kSec, 1}).accepted);
    EXPECT_DOUBLE_EQ(engine.statuses().at(0).arrival_mid, (99.5 + 100.3) / 2.0);
    const auto fills = engine.step(4 * kSec);
    ASSERT_EQ(fills.size(), 1u);  // one leg per venue; both coinbase levels merge into one leg
    EXPECT_EQ(fills[0].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[0].qty, 1.0);
    EXPECT_DOUBLE_EQ(fills[0].price, (0.5 * 100.3 + 0.5 * 100.6) / 1.0);
    EXPECT_FALSE(engine.statuses().at(0).venue_costs[0].available);
}

TEST_F(RoutingTest, VenueWithItsOwnBookCrossedSkipsTheStep) {
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    ASSERT_TRUE(engine.apply_book_update(0, {{100.5, 1.0}}, {}, kSec));
    EXPECT_TRUE(engine.step(kSec).empty());
    ASSERT_TRUE(engine.apply_book_update(0, {{100.5, 0.0}}, {}, kSec));
    EXPECT_EQ(engine.step(kSec).size(), 2u);
}

TEST_F(RoutingTest, CrossBetweenVenuesWithinFeesStillTrades) {
    // Coinbase bid 100.1 is above Kraken ask 100.0 gross, but Kraken's 40 bps fee makes that
    // ask 100.4 effective, so there is no arbitrage: this is how live venues normally look.
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{100.1, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, kSec));
    const auto submitted = engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1});
    ASSERT_TRUE(submitted.accepted) << submitted.reason;
    EXPECT_DOUBLE_EQ(engine.statuses().at(0).arrival_mid, (100.1 + 100.0) / 2.0);
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[1].qty, 0.5);
}

TEST_F(RoutingTest, CrossBetweenVenuesBeyondFeesSkipsTheStep) {
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1}).accepted);
    // Coinbase bid 100.45 (no fee) beats Kraken's effective ask 100.0 x 1.004 = 100.4:
    // an arbitrage after fees, which real venues do not leave standing, so the data is suspect.
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{100.45, 5.0}}, {{100.6, 5.0}}, kSec));
    EXPECT_TRUE(engine.step(kSec).empty());
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.6, 5.0}}, kSec));
    EXPECT_EQ(engine.step(kSec).size(), 1u);
}

TEST_F(RoutingTest, SubmitRejectsWhenCrossedBeyondFees) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{100.45, 5.0}}, {{100.6, 5.0}}, kSec));
    const auto submitted = engine.submit({"o", Side::Buy, 1.0, kSec, kSec, 1});
    EXPECT_FALSE(submitted.accepted);
    EXPECT_EQ(submitted.reason, "no market data");
}

TEST_F(RoutingTest, RejectsUnknownVenueIndexAndNegativeRecvTime) {
    EXPECT_FALSE(engine.apply_book_snapshot(2, {{99.0, 1.0}}, {{100.0, 1.0}}, kSec));
    EXPECT_FALSE(engine.apply_book_snapshot(0, {{99.0, 1.0}}, {{100.0, 1.0}}, -1));
    EXPECT_EQ(engine.venue_index("coinbase"), std::optional<std::size_t>{1});
    EXPECT_FALSE(engine.venue_index("binance").has_value());
    EXPECT_EQ(engine.venue_count(), 2u);
}

TEST(RoutingSingleVenue, LegacyEngineIsUnchangedAndNeverStale) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 5.0}}, {{101.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 1000 * kSec, kSec, 1}).accepted);
    const auto fills = engine.step(1000 * kSec);
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].fee, 0.0);
    const auto status = engine.statuses().at(0);
    EXPECT_DOUBLE_EQ(status.routed_all_in_bps, status.slippage_bps);
}

TEST_F(RoutingTest, BackwardClockCannotReviveStaleVenue) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, 4 * kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 1.0, 0, kSec, 1}).accepted);
    EXPECT_DOUBLE_EQ(engine.statuses().at(0).arrival_mid, (99.5 + 100.3) / 2.0);
    // kraken was last updated at 1 s and the engine has already seen 4 s: an older caller
    // clock must not make the kraken book fresh again.
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_EQ(fills[0].venue, "coinbase");
}

TEST_F(RoutingTest, SellSplitsAcrossVenuesByFeeAdjustedBids) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 0.5}, {98.0, 5.0}}, {{100.3, 0.5}}, kSec));
    ASSERT_TRUE(engine.submit({"s", Side::Sell, 1.0, kSec, kSec, 1}).accepted);
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 2u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[0].price, 99.0);
    EXPECT_NEAR(fills[0].fee, 0.5 * 99.0 * 0.004, 1e-12);
    EXPECT_EQ(fills[1].venue, "coinbase");
    EXPECT_DOUBLE_EQ(fills[1].qty, 0.5);
    EXPECT_DOUBLE_EQ(fills[1].price, 99.5);
    EXPECT_DOUBLE_EQ(engine.position(), -1.0);
}

TEST_F(RoutingTest, VenueWithEmptyAskSideIsSkippedForBuys) {
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {}, kSec));
    ASSERT_TRUE(engine.submit({"o", Side::Buy, 0.5, kSec, kSec, 1}).accepted);
    const auto fills = engine.step(kSec);
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_EQ(fills[0].venue, "kraken");
    EXPECT_FALSE(engine.statuses().at(0).venue_costs[1].available);
}
