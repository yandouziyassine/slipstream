#include <grpcpp/grpcpp.h>
#include <gtest/gtest.h>

#include <chrono>
#include <cstdint>
#include <functional>
#include <future>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "engine_loop.h"
#include "service.h"

using namespace slipstream;
using namespace std::chrono_literals;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;
constexpr std::int64_t kDay = 86'400 * kSec;

// A real gRPC server in this process, reached through its in-process channel.
class Harness {
public:
    explicit Harness(ClockMode mode, std::size_t market_capacity = 10'000,
                     std::size_t subscriber_capacity = 10'000)
        : engine_(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  2 * kSec),
          loop_(engine_, mode, market_capacity, subscriber_capacity),
          service_(loop_, "BTC/USD") {
        grpc::ServerBuilder builder;
        builder.RegisterService(&service_);
        server_ = builder.BuildAndStart();
        stub_ = v1::ExecutionEngine::NewStub(server_->InProcessChannel(grpc::ChannelArguments{}));
    }

    ~Harness() {
        server_->Shutdown(std::chrono::system_clock::now() + 2s);
        loop_.stop();
    }

    Harness(const Harness&) = delete;
    Harness& operator=(const Harness&) = delete;

    v1::ExecutionEngine::Stub& stub() { return *stub_; }
    EngineLoop& loop() { return loop_; }

    v1::StatusReply status() {
        grpc::ClientContext context;
        v1::StatusReply reply;
        EXPECT_TRUE(stub_->GetStatus(&context, v1::StatusRequest{}, &reply).ok());
        return reply;
    }

    // Polls GetStatus: commands run ahead of queued market items, so one call is not a barrier.
    bool eventually(const std::function<bool(const v1::StatusReply&)>& done) {
        const auto deadline = std::chrono::steady_clock::now() + 10s;
        while (std::chrono::steady_clock::now() < deadline) {
            if (done(status())) return true;
            std::this_thread::sleep_for(2ms);
        }
        return false;
    }

    v1::SubmitReply submit(const std::string& id, std::int64_t start_ns, std::int64_t duration_ns,
                           int slices) {
        v1::ParentOrder order;
        order.set_order_id(id);
        order.set_side(v1::SIDE_BUY);
        order.set_qty(1.0);
        order.set_start_ns(start_ns);
        order.set_duration_ns(duration_ns);
        order.set_num_slices(slices);
        grpc::ClientContext context;
        v1::SubmitReply reply;
        EXPECT_TRUE(stub_->SubmitParentOrder(&context, order, &reply).ok());
        return reply;
    }

private:
    Engine engine_;
    EngineLoop loop_;
    ExecutionService service_;
    std::unique_ptr<grpc::Server> server_;
    std::unique_ptr<v1::ExecutionEngine::Stub> stub_;
};

class Stream {
public:
    Stream(v1::ExecutionEngine::Stub& stub, const std::optional<std::string>& venue) {
        context_.set_deadline(std::chrono::system_clock::now() + 30s);
        if (venue) context_.AddMetadata("slipstream-venue", *venue);
        writer_ = stub.MarketStream(&context_, &summary_);
    }

    // The result is ignored on purpose: after the server ends the call, writes just fail and
    // finish() reports the status.
    void send(const v1::MarketEvent& event) { (void)writer_->Write(event); }

    grpc::Status finish() {
        (void)writer_->WritesDone();
        return writer_->Finish();
    }

    std::uint64_t events() const { return summary_.events(); }

private:
    grpc::ClientContext context_;
    v1::MarketStreamSummary summary_;
    std::unique_ptr<grpc::ClientWriter<v1::MarketEvent>> writer_;
};

class Subscription {
public:
    explicit Subscription(v1::ExecutionEngine::Stub& stub) {
        context_.set_deadline(std::chrono::system_clock::now() + 30s);
        reader_ = stub.Subscribe(&context_, v1::SubscribeRequest{});
    }

    // The server sends initial metadata once the subscription is active.
    void wait_active() { reader_->WaitForInitialMetadata(); }

    std::optional<v1::EngineEvent> next() {
        v1::EngineEvent event;
        if (!reader_->Read(&event)) return std::nullopt;
        return event;
    }

    grpc::Status finish() { return reader_->Finish(); }

    grpc::Status cancel() {
        context_.TryCancel();
        while (next()) {
        }
        return finish();
    }

private:
    grpc::ClientContext context_;
    std::unique_ptr<grpc::ClientReader<v1::EngineEvent>> reader_;
};

v1::MarketEvent book(const std::string& venue, std::int64_t recv_ns, double bid_qty,
                     bool snapshot = true) {
    v1::MarketEvent event;
    auto* update = event.mutable_book();
    update->set_symbol("BTC/USD");
    update->set_is_snapshot(snapshot);
    update->set_venue(venue);
    update->set_recv_ns(recv_ns);
    auto* bid = update->add_bids();
    bid->set_price(99.0);
    bid->set_qty(bid_qty);
    if (snapshot) {
        auto* ask = update->add_asks();
        ask->set_price(101.0);
        ask->set_qty(100.0);
    }
    return event;
}

v1::MarketEvent tick(std::int64_t now_ns) {
    v1::MarketEvent event;
    event.mutable_tick()->set_now_ns(now_ns);
    return event;
}

double bid_qty(const v1::StatusReply& status, int venue) {
    const auto& bids = status.books(venue).bids();
    return bids.empty() ? 0.0 : bids.at(0).qty();
}

std::function<bool(const v1::StatusReply&)> booked(int venue) {
    return [venue](const v1::StatusReply& status) { return status.venues(venue).has_book(); };
}

grpc::Status stream_one(Harness& harness, const std::optional<std::string>& venue,
                        const v1::MarketEvent& event) {
    Stream stream(harness.stub(), venue);
    stream.send(event);
    return stream.finish();
}

}  // namespace

TEST(StreamTest, TwoVenuesStreamConcurrently) {
    Harness harness(ClockMode::Live);
    constexpr int kUpdates = 300;
    const auto feed = [&](const std::string& venue, double scale) {
        Stream stream(harness.stub(), venue);
        stream.send(book(venue, 0, scale));
        for (int i = 1; i <= kUpdates; ++i) stream.send(book("", 0, scale * i, false));
        const auto status = stream.finish();
        EXPECT_TRUE(status.ok()) << status.error_message();
        EXPECT_EQ(stream.events(), static_cast<std::uint64_t>(kUpdates + 1));
    };
    auto kraken = std::async(std::launch::async, feed, "kraken", 1.0);
    auto coinbase = std::async(std::launch::async, feed, "coinbase", 2.0);
    kraken.get();
    coinbase.get();

    EXPECT_TRUE(harness.eventually([](const v1::StatusReply& status) {
        return bid_qty(status, 0) == 1.0 * kUpdates && bid_qty(status, 1) == 2.0 * kUpdates;
    }));
    const auto status = harness.status();
    EXPECT_TRUE(status.venues(0).has_book());
    EXPECT_TRUE(status.venues(1).has_book());
    EXPECT_TRUE(status.venues(0).fresh());
    EXPECT_EQ(status.clock_mode(), v1::CLOCK_MODE_LIVE);
}

TEST(StreamTest, LiveRejectsMissingUnknownAndDuplicateVenues) {
    Harness harness(ClockMode::Live);
    EXPECT_EQ(stream_one(harness, std::nullopt, book("", 0, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(stream_one(harness, "binance", book("", 0, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(stream_one(harness, "", book("", 0, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    Stream first(harness.stub(), "kraken");
    first.send(book("", 0, 1.0));
    ASSERT_TRUE(harness.eventually(booked(0)));
    EXPECT_EQ(stream_one(harness, "kraken", book("", 0, 2.0)).error_code(),
              grpc::StatusCode::FAILED_PRECONDITION);
    EXPECT_TRUE(first.finish().ok());
    EXPECT_EQ(first.events(), 1u);

    // The first stream unbound its venue when it ended.
    EXPECT_TRUE(stream_one(harness, "kraken", book("", 0, 3.0)).ok());
}

TEST(StreamTest, LiveRejectsClientTime) {
    Harness harness(ClockMode::Live);
    EXPECT_EQ(stream_one(harness, "kraken", book("", 5, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(stream_one(harness, "kraken", tick(5)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(stream_one(harness, "kraken", book("coinbase", 0, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_FALSE(harness.status().venues(0).has_book());
    EXPECT_FALSE(harness.status().venues(1).has_book());
}

TEST(StreamTest, ReplayRejectsBadTimeAndMissingVenue) {
    Harness harness(ClockMode::Replay);
    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(book("kraken", 2 * kSec, 1.0));
        stream.send(book("kraken", kSec, 1.0));
        EXPECT_EQ(stream.finish().error_code(), grpc::StatusCode::INVALID_ARGUMENT);
    }
    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(tick(kSec));
        stream.send(tick(kSec + kDay + 1));
        EXPECT_EQ(stream.finish().error_code(), grpc::StatusCode::INVALID_ARGUMENT);
    }
    EXPECT_EQ(stream_one(harness, std::nullopt, book("", kSec, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);
    EXPECT_EQ(stream_one(harness, std::nullopt, book("kraken", 0, 1.0)).error_code(),
              grpc::StatusCode::INVALID_ARGUMENT);

    // Each rejected stream released the replay binding.
    Stream good(harness.stub(), std::nullopt);
    good.send(book("coinbase", 3 * kSec, 4.0));
    const auto status = good.finish();
    EXPECT_TRUE(status.ok()) << status.error_message();
    EXPECT_EQ(good.events(), 1u);
    EXPECT_TRUE(harness.eventually([](const v1::StatusReply& s) { return bid_qty(s, 1) == 4.0; }));
    EXPECT_EQ(harness.status().clock_mode(), v1::CLOCK_MODE_REPLAY);
}

TEST(StreamTest, ReplayAllowsOneStreamAtATime) {
    Harness harness(ClockMode::Replay);
    Stream first(harness.stub(), std::nullopt);
    first.send(book("kraken", kSec, 1.0));
    ASSERT_TRUE(harness.eventually(booked(0)));
    EXPECT_EQ(stream_one(harness, std::nullopt, book("coinbase", kSec, 1.0)).error_code(),
              grpc::StatusCode::FAILED_PRECONDITION);
    EXPECT_TRUE(first.finish().ok());
}

TEST(StreamTest, InvalidEventEndsOnlyItsOwnStream) {
    Harness harness(ClockMode::Live);
    Stream kraken(harness.stub(), "kraken");
    Stream coinbase(harness.stub(), "coinbase");
    coinbase.send(book("", 0, 1.0));
    kraken.send(book("", 0, 1.0));
    ASSERT_TRUE(harness.eventually([](const v1::StatusReply& s) {
        return s.venues(0).has_book() && s.venues(1).has_book();
    }));

    auto bad = book("", 0, 2.0, false);
    bad.mutable_book()->mutable_bids(0)->set_price(-1.0);
    kraken.send(bad);
    EXPECT_EQ(kraken.finish().error_code(), grpc::StatusCode::INVALID_ARGUMENT);

    coinbase.send(book("", 0, 7.0, false));
    const auto status = coinbase.finish();
    EXPECT_TRUE(status.ok()) << status.error_message();
    EXPECT_EQ(coinbase.events(), 2u);
    EXPECT_TRUE(harness.eventually([](const v1::StatusReply& s) {
        return bid_qty(s, 1) == 7.0 && bid_qty(s, 0) == 1.0;
    }));
}

TEST(StreamTest, FullEngineQueueEndsTheStreamWithResourceExhausted) {
    Harness harness(ClockMode::Replay, 1);
    std::promise<void> release;
    std::shared_future<void> released = release.get_future().share();
    std::promise<void> entered;
    auto gate = std::async(std::launch::async, [&] {
        harness.loop().run([&](Engine&) {
            entered.set_value();
            released.wait();
        });
    });
    entered.get_future().wait();

    Stream stream(harness.stub(), std::nullopt);
    for (int i = 1; i <= 3; ++i) stream.send(book("kraken", i * kSec, 1.0));
    const auto status = stream.finish();
    release.set_value();
    gate.get();
    EXPECT_EQ(status.error_code(), grpc::StatusCode::RESOURCE_EXHAUSTED);
}

TEST(StreamTest, SubscriberReceivesFillsAndTheTerminalUpdate) {
    Harness harness(ClockMode::Replay);
    Subscription subscription(harness.stub());
    subscription.wait_active();
    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(book("kraken", kSec, 1.0));
        ASSERT_TRUE(stream.finish().ok());
    }
    ASSERT_TRUE(harness.eventually(booked(0)));

    const auto reply = harness.submit("o-1", kSec, kSec, 2);
    ASSERT_TRUE(reply.accepted()) << reply.reason();

    // With a subscriber active, the first slice executes at submission, before any market event.
    const auto first = subscription.next();
    ASSERT_TRUE(first);
    ASSERT_TRUE(first->has_fill());
    EXPECT_EQ(first->seq(), 1u);
    EXPECT_EQ(first->fill().order_id(), "o-1");
    EXPECT_DOUBLE_EQ(first->fill().qty(), 0.5);
    EXPECT_EQ(first->fill().ts_ns(), kSec);
    EXPECT_EQ(first->fill().venue(), "kraken");

    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(tick(2 * kSec));
        ASSERT_TRUE(stream.finish().ok());
    }
    double filled = first->fill().qty();
    std::uint64_t last_seq = first->seq();
    std::optional<v1::OrderUpdate> terminal;
    while (!terminal) {
        const auto event = subscription.next();
        ASSERT_TRUE(event) << "subscription ended early";
        EXPECT_GT(event->seq(), last_seq);
        last_seq = event->seq();
        if (event->has_fill()) filled += event->fill().qty();
        if (event->has_order() && event->order().state() != v1::ORDER_STATE_WORKING) {
            terminal = event->order();
        }
    }
    EXPECT_EQ(terminal->order_id(), "o-1");
    EXPECT_EQ(terminal->state(), v1::ORDER_STATE_COMPLETED);
    EXPECT_DOUBLE_EQ(terminal->filled_qty(), 1.0);
    EXPECT_DOUBLE_EQ(filled, 1.0);

    const auto ended = subscription.cancel();
    EXPECT_TRUE(ended.ok() || ended.error_code() == grpc::StatusCode::CANCELLED)
        << ended.error_message();
}

TEST(StreamTest, SecondSubscriberIsRejectedUntilTheFirstLeaves) {
    Harness harness(ClockMode::Replay);
    auto first = std::make_unique<Subscription>(harness.stub());
    first->wait_active();
    {
        Subscription second(harness.stub());
        EXPECT_FALSE(second.next());
        EXPECT_EQ(second.finish().error_code(), grpc::StatusCode::FAILED_PRECONDITION);
    }
    (void)first->cancel();
    first.reset();

    // The first handler unsubscribes within one poll interval of noticing the cancellation.
    const auto deadline = std::chrono::steady_clock::now() + 10s;
    std::shared_ptr<Subscriber> probe;
    while (!(probe = harness.loop().subscribe()) && std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(5ms);
    }
    ASSERT_NE(probe, nullptr);
    harness.loop().unsubscribe(probe);

    Subscription third(harness.stub());
    third.wait_active();
    {
        Subscription fourth(harness.stub());
        EXPECT_FALSE(fourth.next());
        EXPECT_EQ(fourth.finish().error_code(), grpc::StatusCode::FAILED_PRECONDITION);
    }
    (void)third.cancel();
}

TEST(StreamTest, SlowSubscriberIsDisconnectedWithResourceExhausted) {
    Harness harness(ClockMode::Replay, 10'000, 1);
    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(book("kraken", kSec, 1.0));
        ASSERT_TRUE(stream.finish().ok());
    }
    ASSERT_TRUE(harness.eventually(booked(0)));
    // Without a subscriber nothing steps, so all orders fill together at the next tick.
    for (int i = 0; i < 50; ++i) {
        ASSERT_TRUE(harness.submit("o-" + std::to_string(i), kSec, kSec, 1).accepted());
    }

    Subscription subscription(harness.stub());
    subscription.wait_active();
    {
        Stream stream(harness.stub(), std::nullopt);
        stream.send(tick(kSec));
        ASSERT_TRUE(stream.finish().ok());
    }
    while (subscription.next()) {
    }
    EXPECT_EQ(subscription.finish().error_code(), grpc::StatusCode::RESOURCE_EXHAUSTED);
}
