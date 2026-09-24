#include "service.h"

#include <gtest/gtest.h>

#include <string>

using namespace slipstream;

namespace {

class ServiceTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10};
    ExecutionService service{engine, "BTC/USD"};

    static v1::BookUpdate snapshot(const std::string& symbol = "BTC/USD") {
        v1::BookUpdate update;
        update.set_symbol(symbol);
        update.set_is_snapshot(true);
        auto* bid = update.add_bids();
        bid->set_price(99.0);
        bid->set_qty(5.0);
        auto* ask = update.add_asks();
        ask->set_price(101.0);
        ask->set_qty(1.0);
        return update;
    }

    static v1::ParentOrder order(v1::Side side) {
        v1::ParentOrder o;
        o.set_order_id("o-1");
        o.set_side(side);
        o.set_qty(1.0);
        o.set_start_ns(0);
        o.set_duration_ns(1'000'000'000);
        o.set_num_slices(1);
        return o;
    }
};

}  // namespace

TEST_F(ServiceTest, RejectsUnexpectedSymbol) {
    const auto update = snapshot("ETH/USD");
    v1::BookAck ack;
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, RejectsInvalidLevel) {
    auto update = snapshot();
    update.mutable_bids(0)->set_price(-1.0);
    v1::BookAck ack;
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, RejectsTooManyLevels) {
    auto update = snapshot();
    for (int i = 0; i < ExecutionService::kMaxLevelsPerUpdate; ++i) {
        auto* bid = update.add_bids();
        bid->set_price(1.0 + i);
        bid->set_qty(1.0);
    }
    v1::BookAck ack;
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, RejectsUnspecifiedSide) {
    const auto request = order(v1::SIDE_UNSPECIFIED);
    v1::SubmitReply reply;
    EXPECT_EQ(service.SubmitParentOrder(nullptr, &request, &reply).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, BusinessRejectionIsNotAnRpcError) {
    const auto request = order(v1::SIDE_BUY);
    v1::SubmitReply reply;
    EXPECT_TRUE(service.SubmitParentOrder(nullptr, &request, &reply).ok());
    EXPECT_FALSE(reply.accepted());
    EXPECT_EQ(reply.reason(), "no market data");
}

TEST_F(ServiceTest, SubmitStepAndStatusRoundTrip) {
    const auto update = snapshot();
    v1::BookAck ack;
    ASSERT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());

    const auto request = order(v1::SIDE_BUY);
    v1::SubmitReply submit_reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &request, &submit_reply).ok());
    ASSERT_TRUE(submit_reply.accepted()) << submit_reply.reason();

    v1::StepRequest step;
    step.set_now_ns(0);
    v1::StepReply step_reply;
    ASSERT_TRUE(service.Step(nullptr, &step, &step_reply).ok());
    ASSERT_EQ(step_reply.fills_size(), 1);
    EXPECT_DOUBLE_EQ(step_reply.fills(0).price(), 101.0);

    v1::StatusRequest status_request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &status_request, &status).ok());
    EXPECT_DOUBLE_EQ(status.position(), 1.0);
    EXPECT_TRUE(status.has_mid());
    EXPECT_DOUBLE_EQ(status.mid(), 100.0);
    ASSERT_EQ(status.orders_size(), 1);
    EXPECT_EQ(status.orders(0).order_id(), "o-1");
    EXPECT_EQ(status.orders(0).state(), v1::ORDER_STATE_COMPLETED);
}
