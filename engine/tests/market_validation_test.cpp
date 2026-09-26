#include "market_validation.h"

#include <gtest/gtest.h>

#include <cstdint>
#include <limits>
#include <string>
#include <variant>

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;
constexpr std::int64_t kDay = 86'400 * kSec;

v1::BookUpdate book_update(const std::string& venue, std::int64_t recv_ns, bool snapshot = true) {
    v1::BookUpdate update;
    update.set_symbol("BTC/USD");
    update.set_is_snapshot(snapshot);
    update.set_venue(venue);
    update.set_recv_ns(recv_ns);
    auto* bid = update.add_bids();
    bid->set_price(99.0);
    bid->set_qty(1.0);
    auto* ask = update.add_asks();
    ask->set_price(101.0);
    ask->set_qty(1.0);
    return update;
}

v1::MarketEvent book_event(const std::string& venue, std::int64_t recv_ns) {
    v1::MarketEvent event;
    *event.mutable_book() = book_update(venue, recv_ns);
    return event;
}

v1::MarketEvent trade_event(const std::string& venue) {
    v1::MarketEvent event;
    auto* batch = event.mutable_trades();
    batch->set_symbol("BTC/USD");
    batch->set_venue(venue);
    auto* trade = batch->add_trades();
    trade->set_price(100.0);
    trade->set_qty(0.5);
    return event;
}

v1::MarketEvent tick_event(std::int64_t now_ns) {
    v1::MarketEvent event;
    event.mutable_tick()->set_now_ns(now_ns);
    return event;
}

v1::MarketEvent heartbeat_event() {
    v1::MarketEvent event;
    event.mutable_heartbeat();
    return event;
}

grpc::StatusCode code(const Admission& admission) {
    if (const auto* status = std::get_if<grpc::Status>(&admission)) return status->error_code();
    return grpc::StatusCode::OK;
}

const MarketItem& item(const Admission& admission) { return std::get<MarketItem>(admission); }

// A book event with `levels` bids, all at distinct valid prices.
v1::MarketEvent wide_book_event(const std::string& venue, std::int64_t recv_ns, int levels) {
    v1::MarketEvent event;
    auto* update = event.mutable_book();
    update->set_symbol("BTC/USD");
    update->set_is_snapshot(true);
    update->set_venue(venue);
    update->set_recv_ns(recv_ns);
    for (int i = 0; i < levels; ++i) {
        auto* bid = update->add_bids();
        bid->set_price(1.0 + i);
        bid->set_qty(1.0);
    }
    return event;
}

class MarketValidationTest : public ::testing::Test {
protected:
    MarketValidator validator{"BTC/USD", {"kraken", "coinbase"}, 10};
};

}  // namespace

TEST_F(MarketValidationTest, ResolvesExactVenueNames) {
    EXPECT_EQ(validator.resolve_venue("coinbase"), 1u);
    EXPECT_FALSE(validator.resolve_venue("Kraken"));
    EXPECT_FALSE(validator.resolve_venue(""));  // ambiguous with two venues
    MarketValidator single{"BTC/USD", {"kraken"}, 10};
    EXPECT_EQ(single.resolve_venue(""), 0u);
}

TEST_F(MarketValidationTest, AdmitsAValidBook) {
    const auto admitted = validator.book(book_update("", 7), 1);
    ASSERT_EQ(code(admitted), grpc::StatusCode::OK);
    EXPECT_EQ(item(admitted).venue, 1u);
    const auto& book = std::get<BookData>(item(admitted).data);
    EXPECT_TRUE(book.is_snapshot);
    EXPECT_EQ(book.recv_ns, 7);
    ASSERT_EQ(book.bids.size(), 1u);
    EXPECT_DOUBLE_EQ(book.bids[0].price, 99.0);
}

TEST_F(MarketValidationTest, RejectsBadBooks) {
    auto wrong_symbol = book_update("", 0);
    wrong_symbol.set_symbol("ETH/USD");
    EXPECT_EQ(code(validator.book(wrong_symbol, 0)), grpc::StatusCode::INVALID_ARGUMENT);

    EXPECT_EQ(code(validator.book(book_update("", -1), 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto too_many = book_update("", 0);
    for (int i = 0; i < MarketValidator::kMaxLevelsPerUpdate; ++i) {
        auto* ask = too_many.add_asks();
        ask->set_price(200.0 + i);
        ask->set_qty(1.0);
    }
    EXPECT_EQ(code(validator.book(too_many, 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto nan_price = book_update("", 0);
    nan_price.mutable_bids(0)->set_price(std::numeric_limits<double>::quiet_NaN());
    EXPECT_EQ(code(validator.book(nan_price, 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto infinite_qty = book_update("", 0);
    infinite_qty.mutable_asks(0)->set_qty(std::numeric_limits<double>::infinity());
    EXPECT_EQ(code(validator.book(infinite_qty, 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto zero_price = book_update("", 0);
    zero_price.mutable_asks(0)->set_price(0.0);
    EXPECT_EQ(code(validator.book(zero_price, 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto negative_qty = book_update("", 0, false);
    negative_qty.mutable_bids(0)->set_qty(-1.0);
    EXPECT_EQ(code(validator.book(negative_qty, 0)), grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(MarketValidationTest, ZeroQuantityDeletesInAnUpdateOnly) {
    auto snapshot = book_update("", 0, true);
    snapshot.mutable_bids(0)->set_qty(0.0);
    EXPECT_EQ(code(validator.book(snapshot, 0)), grpc::StatusCode::INVALID_ARGUMENT);
    auto update = book_update("", 0, false);
    update.mutable_bids(0)->set_qty(0.0);
    EXPECT_EQ(code(validator.book(update, 0)), grpc::StatusCode::OK);
}

TEST_F(MarketValidationTest, ChecksTrades) {
    const auto good = trade_event("");
    const auto admitted = validator.trades(good.trades(), 0);
    ASSERT_EQ(code(admitted), grpc::StatusCode::OK);
    ASSERT_EQ(std::get<TradeData>(item(admitted).data).trades.size(), 1u);

    auto zero_qty = good;
    zero_qty.mutable_trades()->mutable_trades(0)->set_qty(0.0);
    EXPECT_EQ(code(validator.trades(zero_qty.trades(), 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto nan_price = good;
    nan_price.mutable_trades()->mutable_trades(0)->set_price(
        std::numeric_limits<double>::quiet_NaN());
    EXPECT_EQ(code(validator.trades(nan_price.trades(), 0)), grpc::StatusCode::INVALID_ARGUMENT);

    auto wrong_symbol = good;
    wrong_symbol.mutable_trades()->set_symbol("ETH/USD");
    EXPECT_EQ(code(validator.trades(wrong_symbol.trades(), 0)),
              grpc::StatusCode::INVALID_ARGUMENT);

    auto too_many = good;
    for (int i = 0; i < MarketValidator::kMaxTradesPerBatch; ++i) {
        *too_many.mutable_trades()->add_trades() = good.trades().trades(0);
    }
    EXPECT_EQ(code(validator.trades(too_many.trades(), 0)), grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(MarketValidationTest, LiveStreamBindsEveryEventToItsVenue) {
    auto stream = StreamAdmission::live(validator, 1);
    const auto unnamed = stream.admit(book_event("", 0));
    ASSERT_EQ(code(unnamed), grpc::StatusCode::OK);
    EXPECT_EQ(item(unnamed).venue, 1u);
    EXPECT_EQ(code(stream.admit(book_event("coinbase", 0))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(book_event("kraken", 0))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(trade_event("coinbase"))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(trade_event("kraken"))), grpc::StatusCode::INVALID_ARGUMENT);
    const auto heartbeat = stream.admit(heartbeat_event());
    ASSERT_EQ(code(heartbeat), grpc::StatusCode::OK);
    EXPECT_EQ(item(heartbeat).venue, 1u);
    EXPECT_TRUE(std::holds_alternative<HeartbeatData>(item(heartbeat).data));
}

TEST_F(MarketValidationTest, LiveStreamRejectsClientTime) {
    auto stream = StreamAdmission::live(validator, 0);
    EXPECT_EQ(code(stream.admit(book_event("", 5))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(tick_event(5))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(v1::MarketEvent{})), grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(MarketValidationTest, ReplayStreamNeedsVenuesAndTimes) {
    auto stream = StreamAdmission::replay(validator);
    EXPECT_EQ(code(stream.admit(book_event("", kSec))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(book_event("kraken", 0))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(trade_event(""))), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(tick_event(0))), grpc::StatusCode::INVALID_ARGUMENT);
    // A heartbeat has no venue field, so only a single-venue engine can place it.
    EXPECT_EQ(code(stream.admit(heartbeat_event())), grpc::StatusCode::INVALID_ARGUMENT);

    const auto booked = stream.admit(book_event("coinbase", kSec));
    ASSERT_EQ(code(booked), grpc::StatusCode::OK);
    EXPECT_EQ(item(booked).venue, 1u);
    EXPECT_EQ(std::get<BookData>(item(booked).data).recv_ns, kSec);
    const auto traded = stream.admit(trade_event("kraken"));
    ASSERT_EQ(code(traded), grpc::StatusCode::OK);
    EXPECT_EQ(item(traded).venue, 0u);
    const auto ticked = stream.admit(tick_event(2 * kSec));
    ASSERT_EQ(code(ticked), grpc::StatusCode::OK);
    EXPECT_EQ(std::get<TickData>(item(ticked).data).now_ns, 2 * kSec);

    MarketValidator single{"BTC/USD", {"kraken"}, 10};
    auto single_stream = StreamAdmission::replay(single);
    const auto heartbeat = single_stream.admit(heartbeat_event());
    ASSERT_EQ(code(heartbeat), grpc::StatusCode::OK);
    EXPECT_EQ(item(heartbeat).venue, 0u);
}

TEST_F(MarketValidationTest, ReplayTimeNeverGoesBackwards) {
    auto stream = StreamAdmission::replay(validator);
    ASSERT_EQ(code(stream.admit(book_event("kraken", 5 * kSec))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(book_event("kraken", 5 * kSec))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(tick_event(4 * kSec))), grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(MarketValidationTest, ReplayTimeJumpsAtMostOneDay) {
    auto stream = StreamAdmission::replay(validator);
    ASSERT_EQ(code(stream.admit(tick_event(kSec))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(tick_event(kSec + kDay))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(book_event("kraken", 2 * kSec + 2 * kDay))),
              grpc::StatusCode::INVALID_ARGUMENT);
}

TEST_F(MarketValidationTest, ReplayRejectsAnInvalidBookBeforeRecordingItsTime) {
    auto stream = StreamAdmission::replay(validator);
    auto bad = book_event("kraken", 9 * kSec);
    bad.mutable_book()->mutable_bids(0)->set_price(-1.0);
    EXPECT_EQ(code(stream.admit(bad)), grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(code(stream.admit(book_event("kraken", kSec))), grpc::StatusCode::OK);
}

TEST_F(MarketValidationTest, StreamsCapLevelsPerSideAtBookDepthOrOneHundred) {
    auto live = StreamAdmission::live(validator, 0);
    EXPECT_EQ(code(live.admit(wide_book_event("", 0, 100))), grpc::StatusCode::OK);
    EXPECT_EQ(code(live.admit(wide_book_event("", 0, 101))), grpc::StatusCode::INVALID_ARGUMENT);
    auto asks = wide_book_event("", 0, 0);
    for (int i = 0; i < 101; ++i) {
        auto* ask = asks.mutable_book()->add_asks();
        ask->set_price(1.0 + i);
        ask->set_qty(1.0);
    }
    EXPECT_EQ(code(live.admit(asks)), grpc::StatusCode::INVALID_ARGUMENT);

    auto replay = StreamAdmission::replay(validator);
    EXPECT_EQ(code(replay.admit(wide_book_event("kraken", kSec, 100))), grpc::StatusCode::OK);
    EXPECT_EQ(code(replay.admit(wide_book_event("kraken", kSec, 101))),
              grpc::StatusCode::INVALID_ARGUMENT);

    // The unary path keeps its own, wider cap.
    EXPECT_EQ(code(validator.book(wide_book_event("", 0, 101).book(), 0)), grpc::StatusCode::OK);
}

TEST_F(MarketValidationTest, StreamLevelCapFollowsADeeperBook) {
    MarketValidator deep{"BTC/USD", {"kraken"}, 250};
    auto stream = StreamAdmission::live(deep, 0);
    EXPECT_EQ(code(stream.admit(wide_book_event("", 0, 250))), grpc::StatusCode::OK);
    EXPECT_EQ(code(stream.admit(wide_book_event("", 0, 251))), grpc::StatusCode::INVALID_ARGUMENT);
}
