#include "risk.h"

#include <gtest/gtest.h>

#include <cmath>

using slipstream::RiskCheck;
using slipstream::RiskLimits;
using slipstream::Side;

namespace {
const RiskCheck kRisk(RiskLimits{1000.0, 1.0});
}

TEST(Risk, ParentWithinLimitsPasses) {
    EXPECT_TRUE(kRisk.check_parent(Side::Buy, 0.5, 100.0, 0.0).ok);
}

TEST(Risk, ParentNotionalLimit) {
    const auto decision = kRisk.check_parent(Side::Buy, 20.0, 100.0, 0.0);
    EXPECT_FALSE(decision.ok);
    EXPECT_EQ(decision.reason, "order notional limit exceeded");
}

TEST(Risk, ParentPositionLimitIncludesProjectedPosition) {
    const auto decision = kRisk.check_parent(Side::Buy, 0.5, 100.0, 0.6);
    EXPECT_FALSE(decision.ok);
    EXPECT_EQ(decision.reason, "position limit exceeded");
}

TEST(Risk, SellReducesLongPosition) {
    EXPECT_TRUE(kRisk.check_parent(Side::Sell, 0.5, 100.0, 0.9).ok);
}

TEST(Risk, ShortPositionLimit) {
    EXPECT_FALSE(kRisk.check_parent(Side::Sell, 0.5, 100.0, -0.6).ok);
}

TEST(Risk, RejectsInvalidInputs) {
    EXPECT_EQ(kRisk.check_parent(Side::Buy, 0.0, 100.0, 0.0).reason, "invalid quantity");
    EXPECT_EQ(kRisk.check_parent(Side::Buy, std::nan(""), 100.0, 0.0).reason, "invalid quantity");
    EXPECT_EQ(kRisk.check_parent(Side::Buy, 0.5, 0.0, 0.0).reason, "no reference price");
    EXPECT_EQ(kRisk.check_child(Side::Buy, 0.5, std::nan(""), 0.0, 0.0).reason, "invalid fill cost");
    EXPECT_EQ(kRisk.check_child(Side::Buy, 0.5, 0.0, 0.0, 0.0).reason, "invalid fill cost");
    EXPECT_EQ(kRisk.check_child(Side::Buy, 0.0, 50.0, 0.0, 0.0).reason, "invalid quantity");
}

TEST(Risk, ToleratesFloatingPointRoundingAtTheLimit) {
    EXPECT_TRUE(kRisk.check_parent(Side::Buy, 1.0 + 1e-12, 100.0, 0.0).ok);
    EXPECT_FALSE(kRisk.check_parent(Side::Buy, 1.001, 100.0, 0.0).ok);
}

TEST(Risk, ChildPositionLimit) {
    EXPECT_TRUE(kRisk.check_child(Side::Buy, 0.1, 100.0, 0.9, 0.0).ok);
    const auto decision = kRisk.check_child(Side::Buy, 0.2, 100.0, 0.9, 0.0);
    EXPECT_FALSE(decision.ok);
    EXPECT_EQ(decision.reason, "position limit exceeded");
}

TEST(Risk, ChildCostBudgetIncludesAlreadySpent) {
    EXPECT_TRUE(kRisk.check_child(Side::Buy, 0.5, 60.0, 0.0, 940.0).ok);
    const auto decision = kRisk.check_child(Side::Buy, 0.5, 60.5, 0.0, 940.0);
    EXPECT_FALSE(decision.ok);
    EXPECT_EQ(decision.reason, "order notional limit exceeded");
    EXPECT_FALSE(kRisk.check_child(Side::Sell, 0.5, 60.5, 0.0, 940.0).ok);
}
