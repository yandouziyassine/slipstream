#include "engine_loop.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <future>
#include <memory>
#include <stdexcept>
#include <thread>
#include <utility>
#include <variant>
#include <vector>

#include "latency_histogram.h"

using namespace slipstream;
using namespace std::chrono_literals;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

MarketItem book(std::size_t venue, bool snapshot, std::vector<Level> bids,
                std::vector<Level> asks, std::int64_t recv_ns) {
    return {venue, BookData{snapshot, std::move(bids), std::move(asks), recv_ns}, 0};
}

MarketItem deep_book(std::int64_t recv_ns) {
    return book(0, true, {{99.0, 10.0}}, {{101.0, 10.0}}, recv_ns);
}

MarketItem tick(std::int64_t now_ns) { return {0, TickData{now_ns}, 0}; }

// Commands run ahead of queued market items, so a barrier waits for the processed count.
void wait_for_events(EngineLoop& loop, std::uint64_t count) {
    const auto deadline = std::chrono::steady_clock::now() + 5s;
    while (loop.stats().events < count) {
        ASSERT_LT(std::chrono::steady_clock::now(), deadline) << "loop did not process in time";
        std::this_thread::sleep_for(1ms);
    }
}

std::vector<EngineEventData> drain(Subscriber& subscriber) {
    std::vector<EngineEventData> events;
    while (auto event = subscriber.pop_for(0us)) events.push_back(std::move(*event));
    return events;
}

std::vector<Fill> fills_of(const std::vector<EngineEventData>& events) {
    std::vector<Fill> fills;
    for (const auto& event : events) {
        if (const auto* fill = std::get_if<Fill>(&event.event)) fills.push_back(*fill);
    }
    return fills;
}

bool submit(EngineLoop& loop, ParentOrderRequest request) {
    return loop.run([&](Engine& engine) { return engine.submit(request).accepted; });
}

class EngineLoopTest : public ::testing::Test {
protected:
    Engine engine{RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  2 * kSec};
};

}  // namespace

TEST_F(EngineLoopTest, AppliesMarketItemsInOrder) {
    EngineLoop loop(engine, ClockMode::Replay);
    ASSERT_TRUE(loop.push(book(0, true, {{99.0, 1.0}}, {{101.0, 1.0}}, kSec)));
    for (int i = 1; i <= 200; ++i) {
        ASSERT_TRUE(loop.push(book(0, false, {{99.0, static_cast<double>(i)}}, {}, kSec + i)));
    }
    ASSERT_TRUE(loop.push(book(1, true, {{98.0, 2.0}}, {{102.0, 2.0}}, kSec + 300)));
    wait_for_events(loop, 202);

    const auto books = loop.run([](Engine& e) { return e.books(10); });
    ASSERT_EQ(books.size(), 2u);
    ASSERT_EQ(books[0].bids.size(), 1u);
    EXPECT_DOUBLE_EQ(books[0].bids[0].qty, 200.0);
    ASSERT_EQ(books[1].bids.size(), 1u);
    EXPECT_DOUBLE_EQ(books[1].bids[0].price, 98.0);
}

TEST_F(EngineLoopTest, CommandRunsBeforeQueuedMarketItems) {
    EngineLoop loop(engine, ClockMode::Replay);
    std::promise<void> entered;
    std::promise<void> release;
    std::future<void> released = release.get_future();
    auto gate = std::async(std::launch::async, [&] {
        loop.run([&](Engine&) {
            entered.set_value();
            released.wait();
        });
    });
    entered.get_future().wait();

    int pushed = 0;
    for (int i = 0; i < 1000; ++i) pushed += loop.push(deep_book(kSec + i)) ? 1 : 0;
    auto probe = std::async(std::launch::async, [&] {
        return loop.run([](Engine& e) { return e.venue_states(0).at(0).has_book; });
    });
    std::this_thread::sleep_for(100ms);  // lets the probe reach the command queue
    release.set_value();
    gate.get();

    EXPECT_EQ(pushed, 1000);
    EXPECT_FALSE(probe.get());
    wait_for_events(loop, 1000);
}

TEST_F(EngineLoopTest, CommandWakesAnIdleLoopImmediately) {
    EngineLoop loop(engine, ClockMode::Replay);
    std::vector<std::int64_t> latencies_us;
    for (int i = 0; i < 200; ++i) {
        // Longer than the idle poll, so the loop is blocked, and staggered so arrivals do not
        // phase-lock with the poll cycle.
        std::this_thread::sleep_for(2ms + (i % 10) * 97us);
        const auto start = std::chrono::steady_clock::now();
        loop.run([](Engine&) {});
        const auto elapsed = std::chrono::steady_clock::now() - start;
        latencies_us.push_back(
            std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
    }
    std::sort(latencies_us.begin(), latencies_us.end());
    EXPECT_LT(latencies_us.back(), 50'000);
    // Waiting out the 1 ms idle poll would put the median near 500 us.
    EXPECT_LT(latencies_us[latencies_us.size() / 2], 250);
}

TEST_F(EngineLoopTest, BindVenueTwiceFails) {
    EngineLoop loop(engine, ClockMode::Live);
    EXPECT_TRUE(loop.bind_venue(0));
    EXPECT_FALSE(loop.bind_venue(0));
    EXPECT_TRUE(loop.bind_venue(1));
    loop.unbind_venue(0);
    EXPECT_TRUE(loop.bind_venue(0));
}

TEST_F(EngineLoopTest, OnlyOneReplayStreamBinds) {
    EngineLoop loop(engine, ClockMode::Replay);
    EXPECT_TRUE(loop.bind_replay());
    EXPECT_FALSE(loop.bind_replay());
    loop.unbind_replay();
    EXPECT_TRUE(loop.bind_replay());
}

TEST_F(EngineLoopTest, SecondSubscribeReturnsNull) {
    EngineLoop loop(engine, ClockMode::Replay);
    auto first = loop.subscribe();
    ASSERT_NE(first, nullptr);
    EXPECT_EQ(loop.subscribe(), nullptr);
    loop.unsubscribe(first);
    EXPECT_TRUE(first->closed());
    EXPECT_NE(loop.subscribe(), nullptr);
}

TEST_F(EngineLoopTest, FullSubscriberOverflowsAndStopsReceiving) {
    EngineLoop loop(engine, ClockMode::Replay, 10'000, 1);
    auto subscriber = loop.subscribe();
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);
    ASSERT_TRUE(submit(loop, {"o", Side::Buy, 1.0, kSec, kSec, 2}));
    ASSERT_TRUE(loop.push(tick(kSec)));  // one fill and one update
    ASSERT_TRUE(loop.push(tick(2 * kSec)));
    wait_for_events(loop, 3);

    EXPECT_TRUE(subscriber->overflowed());
    const auto events = drain(*subscriber);
    ASSERT_EQ(events.size(), 1u);
    EXPECT_EQ(events[0].seq, 1u);
}

TEST_F(EngineLoopTest, LiveTimerTickSlicesTwapWithoutMarketData) {
    EngineLoop loop(engine, ClockMode::Live);
    auto subscriber = loop.subscribe();
    ASSERT_TRUE(loop.push(deep_book(0)));
    const auto deadline = std::chrono::steady_clock::now() + 5s;
    while (!loop.run([](Engine& e) { return e.venue_states(0).at(0).has_book; })) {
        ASSERT_LT(std::chrono::steady_clock::now(), deadline);
        std::this_thread::sleep_for(1ms);
    }
    ASSERT_TRUE(submit(loop, {"o", Side::Buy, 1.0, loop.live_now_ns(), 200'000'000, 2}));

    double filled = 0.0;
    bool completed = false;
    while (!completed && std::chrono::steady_clock::now() < deadline) {
        const auto event = subscriber->pop_for(10ms);
        if (!event) continue;
        if (const auto* fill = std::get_if<Fill>(&event->event)) filled += fill->qty;
        if (const auto* update = std::get_if<OrderUpdate>(&event->event)) {
            completed = update->state == OrderState::Completed;
        }
    }
    EXPECT_TRUE(completed);
    EXPECT_DOUBLE_EQ(filled, 1.0);
}

TEST_F(EngineLoopTest, ReplayModeHasNoTimer) {
    EngineLoop loop(engine, ClockMode::Replay);
    auto subscriber = loop.subscribe();
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);
    ASSERT_TRUE(submit(loop, {"o", Side::Buy, 1.0, kSec, kSec, 2}));

    std::this_thread::sleep_for(200ms);  // four live tick intervals
    EXPECT_TRUE(drain(*subscriber).empty());
    EXPECT_EQ(loop.stats().events, 1u);

    ASSERT_TRUE(loop.push(tick(kSec)));
    wait_for_events(loop, 2);
    EXPECT_EQ(fills_of(drain(*subscriber)).size(), 1u);
}

TEST_F(EngineLoopTest, ReplayHeartbeatAndTradesDoNotAdvanceTime) {
    EngineLoop loop(engine, ClockMode::Replay);
    auto subscriber = loop.subscribe();
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);
    ASSERT_TRUE(submit(loop, {"o", Side::Buy, 1.0, 2 * kSec, kSec, 1}));

    // A replay heartbeat or trade has no time of its own; its ingest stamp must be ignored.
    ASSERT_TRUE(loop.push({0, HeartbeatData{}, 50 * kSec}));
    ASSERT_TRUE(loop.push({0, TradeData{{{100.0, 1.0}}}, 50 * kSec}));
    ASSERT_TRUE(loop.push(tick(2 * kSec)));
    wait_for_events(loop, 4);

    const auto fills = fills_of(drain(*subscriber));
    ASSERT_FALSE(fills.empty());
    for (const auto& fill : fills) EXPECT_EQ(fill.ts_ns, 2 * kSec);
}

TEST_F(EngineLoopTest, SeqIsStrictlyIncreasingFromOne) {
    EngineLoop loop(engine, ClockMode::Replay);
    auto subscriber = loop.subscribe();
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);
    ASSERT_TRUE(submit(loop, {"a", Side::Buy, 1.0, kSec, kSec, 4}));
    ASSERT_TRUE(submit(loop, {"b", Side::Sell, 1.0, kSec, kSec, 4}));
    for (int k = 0; k <= 4; ++k) ASSERT_TRUE(loop.push(tick(kSec + k * kSec / 4)));
    wait_for_events(loop, 6);

    const auto events = drain(*subscriber);
    ASSERT_GE(events.size(), 8u);
    for (std::size_t i = 0; i < events.size(); ++i) EXPECT_EQ(events[i].seq, i + 1);
}

TEST_F(EngineLoopTest, StatsCountEventsAndLatency) {
    EngineLoop loop(engine, ClockMode::Replay);
    for (int i = 0; i < 3; ++i) ASSERT_TRUE(loop.push(deep_book(kSec + i)));
    wait_for_events(loop, 3);
    const auto stats = loop.stats();
    EXPECT_EQ(stats.events, 3u);
    EXPECT_GE(stats.queue_high_water, 1u);
    EXPECT_GT(stats.p50_ns, 0);
    EXPECT_GE(stats.p99_ns, stats.p50_ns);
}

TEST_F(EngineLoopTest, StopDrainsQueuedItemsAndClosesSubscriber) {
    std::shared_ptr<Subscriber> subscriber;
    {
        EngineLoop loop(engine, ClockMode::Replay);
        subscriber = loop.subscribe();
        ASSERT_TRUE(loop.push(book(0, true, {{99.0, 1.0}}, {{101.0, 1.0}}, kSec)));
        for (int i = 1; i <= 500; ++i) {
            ASSERT_TRUE(loop.push(book(0, false, {{99.0, static_cast<double>(i)}}, {}, kSec)));
        }
        loop.stop();
    }
    EXPECT_TRUE(subscriber->closed());
    EXPECT_DOUBLE_EQ(engine.books(10).at(0).bids.at(0).qty, 500.0);
}

TEST_F(EngineLoopTest, RunAfterStopThrows) {
    EngineLoop loop(engine, ClockMode::Live);
    loop.stop();
    loop.stop();
    EXPECT_FALSE(loop.push(deep_book(0)));
    try {
        loop.run([](Engine&) {});
        FAIL() << "run after stop must throw";
    } catch (const std::runtime_error& error) {
        EXPECT_STREQ(error.what(), "engine loop stopped");
    }
}

TEST_F(EngineLoopTest, RunPropagatesExceptions) {
    EngineLoop loop(engine, ClockMode::Replay);
    EXPECT_THROW(loop.run([](Engine&) -> int { throw std::invalid_argument("bad"); }),
                 std::invalid_argument);
    EXPECT_EQ(loop.run([](Engine&) { return 7; }), 7);
}

TEST(LatencyHistogramTest, EmptyReportsZero) {
    LatencyHistogram histogram;
    EXPECT_EQ(histogram.percentile(50), 0);
    EXPECT_EQ(histogram.percentile(99), 0);
}

TEST(LatencyHistogramTest, ReportsUpperBoundOfContainingBucket) {
    LatencyHistogram histogram;
    for (int i = 0; i < 99; ++i) histogram.record(1000);  // bucket [512, 1024)
    histogram.record(1'000'000);
    EXPECT_EQ(histogram.percentile(50), 1024);
    EXPECT_EQ(histogram.percentile(99), 1024);
    histogram.record(1'000'000);  // bucket [2^19, 2^20)
    EXPECT_EQ(histogram.percentile(99), 1 << 20);
}

TEST(LatencyHistogramTest, ClampsExtremes) {
    LatencyHistogram histogram;
    histogram.record(-5);
    EXPECT_EQ(histogram.percentile(50), 2);
    LatencyHistogram slow;
    slow.record(std::int64_t{1} << 40);
    EXPECT_EQ(slow.percentile(50), std::int64_t{1} << 32);
}

TEST_F(EngineLoopTest, SubmitAndStepFillsTheFirstSliceForASubscriber) {
    EngineLoop loop(engine, ClockMode::Replay);
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);
    auto subscriber = loop.subscribe();

    const auto result = loop.submit_and_step({"o", Side::Buy, 1.0, 2 * kSec, kSec, 2}, TwapSpec{});
    ASSERT_TRUE(result.accepted) << result.reason;

    const auto fills = fills_of(drain(*subscriber));
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_EQ(fills[0].ts_ns, 2 * kSec);
    EXPECT_EQ(loop.inspect([](Engine&, const LoopView& view) { return view.now_ns; }), 2 * kSec);
}

TEST_F(EngineLoopTest, WithoutASubscriberTheLoopNeverSteps) {
    EngineLoop loop(engine, ClockMode::Replay);
    ASSERT_TRUE(loop.push(deep_book(kSec)));
    wait_for_events(loop, 1);

    ASSERT_TRUE(loop.submit_and_step({"o", Side::Buy, 1.0, kSec, kSec, 2}, TwapSpec{}).accepted);
    ASSERT_TRUE(loop.push(tick(2 * kSec)));
    wait_for_events(loop, 2);
    const auto filled = loop.run([](Engine& e) { return e.statuses().at(0).filled_qty; });
    EXPECT_DOUBLE_EQ(filled, 0.0);

    // A legacy client steps the order itself and reads the fills from the step output.
    const auto output = loop.run([](Engine& e) { return e.step(kSec); });
    ASSERT_EQ(output.fills.size(), 1u);
}

TEST_F(EngineLoopTest, LiveSubmitUsesTheEngineClock) {
    EngineLoop loop(engine, ClockMode::Live);
    const auto before = loop.live_now_ns();
    ASSERT_TRUE(loop.push(deep_book(0)));
    const auto deadline = std::chrono::steady_clock::now() + 5s;
    while (!loop.run([](Engine& e) { return e.venue_states(0).at(0).has_book; })) {
        ASSERT_LT(std::chrono::steady_clock::now(), deadline);
        std::this_thread::sleep_for(1ms);
    }
    auto subscriber = loop.subscribe();

    // A client start time of 0 would put the whole schedule in the past.
    const auto result = loop.submit_and_step({"o", Side::Buy, 1.0, 0, 10 * kSec, 2}, TwapSpec{});
    ASSERT_TRUE(result.accepted) << result.reason;

    const auto fills = fills_of(drain(*subscriber));
    ASSERT_EQ(fills.size(), 1u);
    EXPECT_DOUBLE_EQ(fills[0].qty, 0.5);
    EXPECT_GE(fills[0].ts_ns, before);
}

TEST_F(EngineLoopTest, InspectSeesTheEventTimeAndStatsInOneCommand) {
    EngineLoop loop(engine, ClockMode::Replay);
    EXPECT_EQ(loop.mode(), ClockMode::Replay);
    ASSERT_TRUE(loop.push(deep_book(5 * kSec)));
    wait_for_events(loop, 1);
    const auto view = loop.inspect([](Engine& e, const LoopView& v) {
        return std::pair{v, e.venue_states(v.now_ns).at(0).fresh};
    });
    EXPECT_EQ(view.first.now_ns, 5 * kSec);
    EXPECT_EQ(view.first.stats.events, 1u);
    EXPECT_TRUE(view.second);
}

TEST_F(EngineLoopTest, LiveInspectUsesTheLiveClock) {
    EngineLoop loop(engine, ClockMode::Live);
    EXPECT_EQ(loop.mode(), ClockMode::Live);
    const auto before = loop.live_now_ns();
    const auto now = loop.inspect([](Engine&, const LoopView& v) { return v.now_ns; });
    EXPECT_GE(now, before);
    EXPECT_LE(now, loop.live_now_ns());
}
