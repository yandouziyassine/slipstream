#include "service.h"

#include <gtest/gtest.h>

#include <string>

#include "slicing.h"

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
    EXPECT_EQ(status.orders(0).algo(), "twap");
}

TEST_F(ServiceTest, VwapScheduleMapsThroughOneof) {
    const auto update = snapshot();
    v1::BookAck ack;
    ASSERT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());
    auto request = order(v1::SIDE_BUY);
    request.set_num_slices(2);
    request.mutable_vwap()->add_weights(1.0);
    request.mutable_vwap()->add_weights(3.0);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &request, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::StatusRequest status_request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &status_request, &status).ok());
    EXPECT_EQ(status.orders(0).algo(), "vwap");
}

TEST_F(ServiceTest, TooManyVwapWeightsIsInvalidArgument) {
    auto request = order(v1::SIDE_BUY);
    for (int i = 0; i <= kMaxSlices; ++i) request.mutable_vwap()->add_weights(1.0);
    v1::SubmitReply reply;
    EXPECT_EQ(service.SubmitParentOrder(nullptr, &request, &reply).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(ServiceTest, ApplyTradesValidatesBoundary) {
    v1::TradeAck ack;
    v1::TradeBatch wrong_symbol;
    wrong_symbol.set_symbol("ETH/USD");
    EXPECT_EQ(service.ApplyTrades(nullptr, &wrong_symbol, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch too_many;
    too_many.set_symbol("BTC/USD");
    for (int i = 0; i <= ExecutionService::kMaxTradesPerBatch; ++i) {
        auto* trade = too_many.add_trades();
        trade->set_price(100.0);
        trade->set_qty(1.0);
    }
    EXPECT_EQ(service.ApplyTrades(nullptr, &too_many, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch bad;
    bad.set_symbol("BTC/USD");
    auto* negative = bad.add_trades();
    negative->set_price(100.0);
    negative->set_qty(-1.0);
    EXPECT_EQ(service.ApplyTrades(nullptr, &bad, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    v1::TradeBatch good;
    good.set_symbol("BTC/USD");
    auto* trade = good.add_trades();
    trade->set_price(100.0);
    trade->set_qty(2.5);
    EXPECT_TRUE(service.ApplyTrades(nullptr, &good, &ack).ok());
    EXPECT_DOUBLE_EQ(engine.market_volume(), 2.5);
}

TEST_F(ServiceTest, EmptyVenueMeansTheOnlyVenue) {
    const auto update = snapshot();  // venue unset
    v1::BookAck ack;
    EXPECT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());
}

TEST(ServiceMultiVenue, VenueNamesAreValidated) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 0.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    v1::BookUpdate update;
    update.set_symbol("BTC/USD");
    update.set_is_snapshot(true);
    auto* bid = update.add_bids();
    bid->set_price(99.0);
    bid->set_qty(1.0);
    auto* ask = update.add_asks();
    ask->set_price(100.0);
    ask->set_qty(1.0);
    v1::BookAck ack;
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);  // empty venue with two registered
    update.set_venue("Kraken");  // venue names are exact, case-sensitive allowlist entries
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    update.set_venue("binance");
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    update.set_venue("coinbase");
    update.set_recv_ns(-5);
    EXPECT_EQ(service.ApplyBookUpdate(nullptr, &update, &ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    update.set_recv_ns(5);
    EXPECT_TRUE(service.ApplyBookUpdate(nullptr, &update, &ack).ok());

    v1::TradeBatch trades;
    trades.set_symbol("BTC/USD");
    trades.set_venue("binance");
    auto* trade = trades.add_trades();
    trade->set_price(100.0);
    trade->set_qty(1.0);
    v1::TradeAck trade_ack;
    EXPECT_EQ(service.ApplyTrades(nullptr, &trades, &trade_ack).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    trades.set_venue("kraken");
    EXPECT_TRUE(service.ApplyTrades(nullptr, &trades, &trade_ack).ok());
}

TEST(ServiceMultiVenue, StepAndStatusCarryVenueFields) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 0.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    ASSERT_TRUE(engine.apply_book_snapshot(0, {{99.0, 5.0}}, {{100.0, 1.0}}, 0));
    ASSERT_TRUE(engine.apply_book_snapshot(1, {{99.5, 5.0}}, {{100.3, 0.5}, {100.6, 5.0}}, 0));
    v1::ParentOrder order;
    order.set_order_id("o-1");
    order.set_side(v1::SIDE_BUY);
    order.set_qty(1.0);
    order.set_duration_ns(1'000'000'000);
    order.set_num_slices(1);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &order, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::StepRequest step;
    v1::StepReply step_reply;
    ASSERT_TRUE(service.Step(nullptr, &step, &step_reply).ok());
    ASSERT_EQ(step_reply.fills_size(), 2);
    EXPECT_EQ(step_reply.fills(0).venue(), "kraken");
    EXPECT_DOUBLE_EQ(step_reply.fills(0).fee(), 0.2);
    v1::StatusRequest status_request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &status_request, &status).ok());
    EXPECT_DOUBLE_EQ(status.orders(0).fees_paid(), 0.2);
    ASSERT_EQ(status.orders(0).venue_costs_size(), 2);
    EXPECT_EQ(status.orders(0).venue_costs(1).venue(), "coinbase");
    EXPECT_TRUE(status.orders(0).venue_costs(1).available());
}

TEST_F(ServiceTest, PovOrderFillsFromReportedTrades) {
    const auto update = snapshot();
    v1::BookAck book_ack;
    ASSERT_TRUE(service.ApplyBookUpdate(nullptr, &update, &book_ack).ok());
    auto request = order(v1::SIDE_BUY);
    request.mutable_pov()->set_participation(0.5);
    v1::SubmitReply reply;
    ASSERT_TRUE(service.SubmitParentOrder(nullptr, &request, &reply).ok());
    ASSERT_TRUE(reply.accepted()) << reply.reason();
    v1::TradeBatch trades;
    trades.set_symbol("BTC/USD");
    auto* trade = trades.add_trades();
    trade->set_price(100.0);
    trade->set_qty(1.0);
    v1::TradeAck trade_ack;
    ASSERT_TRUE(service.ApplyTrades(nullptr, &trades, &trade_ack).ok());
    v1::StepRequest step;
    step.set_now_ns(0);
    v1::StepReply step_reply;
    ASSERT_TRUE(service.Step(nullptr, &step, &step_reply).ok());
    ASSERT_EQ(step_reply.fills_size(), 1);
    EXPECT_DOUBLE_EQ(step_reply.fills(0).qty(), 0.5);
}

TEST(ServiceMultiVenue, StatusListsVenuesAndFeesInRegistrationOrder) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 40.0}, {"coinbase", 60.0}},
                  2'000'000'000);
    ExecutionService service{engine, "BTC/USD"};
    v1::StatusRequest request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &request, &status).ok());
    ASSERT_EQ(status.venues_size(), 2);
    EXPECT_EQ(status.venues(0).name(), "kraken");
    EXPECT_DOUBLE_EQ(status.venues(0).fee_bps(), 40.0);
    EXPECT_EQ(status.venues(1).name(), "coinbase");
    EXPECT_DOUBLE_EQ(status.venues(1).fee_bps(), 60.0);
}

TEST(ServiceMultiVenue, DefaultEngineListsKrakenWithZeroFee) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10);
    ExecutionService service{engine, "BTC/USD"};
    v1::StatusRequest request;
    v1::StatusReply status;
    ASSERT_TRUE(service.GetStatus(nullptr, &request, &status).ok());
    ASSERT_EQ(status.venues_size(), 1);
    EXPECT_EQ(status.venues(0).name(), "kraken");
    EXPECT_DOUBLE_EQ(status.venues(0).fee_bps(), 0.0);
}
