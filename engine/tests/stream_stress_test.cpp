#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <future>
#include <random>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "engine.h"
#include "stream_harness.h"

using namespace slipstream;
using namespace slipstream::test;

namespace {

constexpr int kDeltas = 50'000;
// Room for every event at once, so the live streams never hit a full queue.
constexpr std::size_t kQueueCapacity = 200'000;
constexpr auto kTimeout = std::chrono::seconds(120);

struct Delta {
    std::vector<Level> bids;
    std::vector<Level> asks;
};

// A deterministic mix of inserts, updates and deletes over 40 price points per side, so levels
// are hit again and again and the depth-10 books keep truncating.
std::vector<Delta> deltas(std::uint32_t seed) {
    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> levels(1, 3);
    std::uniform_int_distribution<int> side(0, 1);
    std::uniform_int_distribution<int> tick(0, 39);
    std::uniform_int_distribution<int> action(0, 2);
    std::uniform_int_distribution<int> qty(1, 100);
    std::vector<Delta> out;
    out.reserve(kDeltas);
    for (int i = 0; i < kDeltas; ++i) {
        Delta delta;
        const int count = levels(rng);
        for (int j = 0; j < count; ++j) {
            const bool bid = side(rng) == 0;
            const double price = (bid ? 90.0 : 100.0) + 0.25 * tick(rng);
            const double size = action(rng) == 0 ? 0.0 : qty(rng);
            (bid ? delta.bids : delta.asks).push_back({price, size});
        }
        out.push_back(std::move(delta));
    }
    return out;
}

const Delta kSnapshot{{{99.0, 1.0}, {98.0, 2.0}, {97.0, 3.0}},
                      {{101.0, 1.0}, {102.0, 2.0}, {103.0, 3.0}}};

v1::MarketEvent book_event(const Delta& delta, bool snapshot) {
    v1::MarketEvent event;
    auto* update = event.mutable_book();
    update->set_symbol("BTC/USD");
    update->set_is_snapshot(snapshot);
    for (const auto& level : delta.bids) {
        auto* bid = update->add_bids();
        bid->set_price(level.price);
        bid->set_qty(level.qty);
    }
    for (const auto& level : delta.asks) {
        auto* ask = update->add_asks();
        ask->set_price(level.price);
        ask->set_qty(level.qty);
    }
    return event;
}

v1::MarketEvent trade_event(double qty) {
    v1::MarketEvent event;
    auto* trade = event.mutable_trades()->add_trades();
    event.mutable_trades()->set_symbol("BTC/USD");
    trade->set_price(100.0);
    trade->set_qty(qty);
    return event;
}

// The snapshot, the deltas, then one trade of marker_qty: the market queue is FIFO and one
// stream pushes in order, so once the marker's volume shows, every book event before it applied.
void feed(Harness& harness, const std::string& venue, const std::vector<Delta>& sequence,
          double marker_qty) {
    Stream stream(harness.stub(), venue, kTimeout);
    stream.send(book_event(kSnapshot, true));
    for (const auto& delta : sequence) stream.send(book_event(delta, false));
    stream.send(trade_event(marker_qty));
    const auto status = stream.finish();
    EXPECT_TRUE(status.ok()) << venue << ": " << status.error_message();
    EXPECT_EQ(stream.events(), static_cast<std::uint64_t>(kDeltas + 2)) << venue;
}

std::vector<VenueBookView> sequential_books(const std::vector<Delta>& kraken,
                                            const std::vector<Delta>& coinbase) {
    Engine engine(RiskLimits{1'000'000.0, 100.0}, 10, {{"kraken", 0.0}, {"coinbase", 0.0}},
                  kStaleNs);
    const std::vector<const std::vector<Delta>*> sequences{&kraken, &coinbase};
    for (std::size_t venue = 0; venue < sequences.size(); ++venue) {
        EXPECT_TRUE(engine.apply_book_snapshot(venue, kSnapshot.bids, kSnapshot.asks, 0));
        for (const auto& delta : *sequences[venue]) {
            EXPECT_TRUE(engine.apply_book_update(venue, delta.bids, delta.asks, 0));
        }
    }
    return engine.books(engine.book_depth());
}

void expect_same_levels(const std::vector<Level>& actual, const std::vector<Level>& expected,
                        const std::string& what) {
    ASSERT_EQ(actual.size(), expected.size()) << what;
    for (std::size_t i = 0; i < actual.size(); ++i) {
        EXPECT_EQ(actual[i].price, expected[i].price) << what << " level " << i;
        EXPECT_EQ(actual[i].qty, expected[i].qty) << what << " level " << i;
    }
}

}  // namespace

TEST(StreamStressTest, ConcurrentVenueStreamsMatchASequentialReplay) {
    const auto kraken = deltas(7);
    const auto coinbase = deltas(11);
    Harness harness(ClockMode::Live, kQueueCapacity);

    // Status reads race the two writers, so ThreadSanitizer also sees the read path.
    std::atomic<bool> streaming{true};
    auto reader = std::async(std::launch::async, [&] {
        while (streaming.load()) (void)harness.status();
    });
    auto kraken_feed =
        std::async(std::launch::async, [&] { feed(harness, "kraken", kraken, 1.0); });
    auto coinbase_feed =
        std::async(std::launch::async, [&] { feed(harness, "coinbase", coinbase, 2.0); });
    kraken_feed.get();
    coinbase_feed.get();
    streaming.store(false);
    reader.get();

    const auto deadline = std::chrono::steady_clock::now() + kTimeout;
    const auto volume = [&] {
        return harness.loop().run([](Engine& engine) { return engine.market_volume(); });
    };
    while (volume() != 3.0 && std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    ASSERT_EQ(volume(), 3.0) << "the engine did not process every streamed event in time";

    const auto actual =
        harness.loop().run([](Engine& engine) { return engine.books(engine.book_depth()); });
    const auto expected = sequential_books(kraken, coinbase);
    ASSERT_EQ(actual.size(), expected.size());
    for (std::size_t venue = 0; venue < actual.size(); ++venue) {
        EXPECT_EQ(actual[venue].venue, expected[venue].venue);
        const auto& name = expected[venue].venue;
        expect_same_levels(actual[venue].bids, expected[venue].bids, name + " bids");
        expect_same_levels(actual[venue].asks, expected[venue].asks, name + " asks");
    }
}
