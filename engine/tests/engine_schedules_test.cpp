#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

#include "engine.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

class EngineSchedulesTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};

    void SetUp() override {
        ASSERT_TRUE(engine.apply_book_snapshot({{99.0, 50.0}}, {{101.0, 50.0}}));
    }
};

}  // namespace

TEST_F(EngineSchedulesTest, DefaultScheduleIsTwap) {
    ASSERT_TRUE(engine.submit({"t", Side::Buy, 1.0, 0, kSec, 1}).accepted);
    EXPECT_EQ(engine.statuses().at(0).algo, "twap");
}

TEST_F(EngineSchedulesTest, VwapOrderFollowsWeights) {
    ASSERT_TRUE(
        engine.submit({"v", Side::Buy, 10.0, 0, 4 * kSec, 4}, VwapSpec{{1.0, 3.0, 4.0, 2.0}})
            .accepted);
    EXPECT_DOUBLE_EQ(engine.step(0).at(0).qty, 1.0);
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 3.0);
    EXPECT_EQ(engine.statuses().at(0).algo, "vwap");
}

TEST_F(EngineSchedulesTest, AlmgrenChrissOrderFrontLoads) {
    const double lambda = 2.0 * (std::cosh(0.5) - 1.0);
    ASSERT_TRUE(engine.submit({"ac", Side::Buy, 1.0, 0, 4 * kSec, 4},
                              AlmgrenChrissSpec{1.0, 1.0, lambda})
                    .accepted);
    EXPECT_NEAR(engine.step(0).at(0).qty, 1.0 - std::sinh(1.5) / std::sinh(2.0), 1e-9);
    EXPECT_EQ(engine.statuses().at(0).algo, "almgren_chriss");
}

TEST_F(EngineSchedulesTest, PovOrderTradesShareOfVolumeSinceSubmit) {
    ASSERT_TRUE(engine.apply_trades({{100.0, 5.0}}));
    ASSERT_TRUE(engine.submit({"p", Side::Buy, 10.0, 0, 10 * kSec, 1}, PovSpec{0.2}).accepted);
    EXPECT_TRUE(engine.step(0).empty());
    ASSERT_TRUE(engine.apply_trades({{100.0, 10.0}, {100.0, 5.0}}));
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 3.0);
    EXPECT_EQ(engine.statuses().at(0).algo, "pov");
}

TEST_F(EngineSchedulesTest, PovOrderHaltsAtDeadline) {
    ASSERT_TRUE(engine.submit({"p", Side::Buy, 10.0, 0, 2 * kSec, 1}, PovSpec{0.2}).accepted);
    ASSERT_TRUE(engine.apply_trades({{100.0, 5.0}}));
    EXPECT_DOUBLE_EQ(engine.step(kSec).at(0).qty, 1.0);
    EXPECT_TRUE(engine.step(2 * kSec).empty());
    const auto status = engine.statuses().at(0);
    EXPECT_EQ(status.state, OrderState::Halted);
    EXPECT_EQ(status.halt_reason, "deadline reached");
    EXPECT_DOUBLE_EQ(status.filled_qty, 1.0);
}

TEST_F(EngineSchedulesTest, RejectsInvalidScheduleSpecs) {
    EXPECT_EQ(engine.submit({"v", Side::Buy, 1.0, 0, 4 * kSec, 4}, VwapSpec{{1.0}}).reason,
              "invalid schedule");
    EXPECT_EQ(engine.submit({"p", Side::Buy, 1.0, 0, kSec, 1}, PovSpec{0.9}).reason,
              "invalid schedule");
    EXPECT_EQ(engine.submit({"a", Side::Buy, 1.0, 0, kSec, 1}, AlmgrenChrissSpec{0.0, 1.0, 1.0})
                  .reason,
              "invalid schedule");
}

TEST_F(EngineSchedulesTest, ApplyTradesValidatesAndAccumulates) {
    EXPECT_FALSE(engine.apply_trades({{100.0, -1.0}}));
    EXPECT_FALSE(engine.apply_trades({{std::nan(""), 1.0}}));
    EXPECT_FALSE(engine.apply_trades({{100.0, 1.0}, {0.0, 1.0}}));
    EXPECT_DOUBLE_EQ(engine.market_volume(), 0.0);
    EXPECT_TRUE(engine.apply_trades({{100.0, 2.0}, {100.0, 3.0}}));
    EXPECT_DOUBLE_EQ(engine.market_volume(), 5.0);
}
