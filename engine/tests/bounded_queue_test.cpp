#include "bounded_queue.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <thread>
#include <utility>
#include <vector>

using slipstream::BoundedQueue;

TEST(BoundedQueueTest, PreservesFifoOrder) {
    BoundedQueue<int> queue(10);
    for (int i = 0; i < 5; ++i) EXPECT_TRUE(queue.try_push(i));
    for (int i = 0; i < 5; ++i) {
        const auto item = queue.try_pop();
        ASSERT_TRUE(item);
        EXPECT_EQ(*item, i);
    }
}

TEST(BoundedQueueTest, EnforcesCapacity) {
    BoundedQueue<int> queue(2);
    EXPECT_TRUE(queue.try_push(1));
    EXPECT_TRUE(queue.try_push(2));
    EXPECT_FALSE(queue.try_push(3));
    EXPECT_EQ(queue.size(), 2u);
    EXPECT_EQ(queue.high_water(), 2u);
}

TEST(BoundedQueueTest, TryPopOnEmptyReturnsNullopt) {
    BoundedQueue<int> queue(4);
    EXPECT_FALSE(queue.try_pop());
}

TEST(BoundedQueueTest, PopForTimesOutWhenEmpty) {
    BoundedQueue<int> queue(4);
    const auto start = std::chrono::steady_clock::now();
    const auto item = queue.pop_for(std::chrono::milliseconds(20));
    const auto elapsed = std::chrono::steady_clock::now() - start;
    EXPECT_FALSE(item);
    EXPECT_GE(elapsed, std::chrono::milliseconds(15));
}

TEST(BoundedQueueTest, CloseWakesABlockedPop) {
    BoundedQueue<int> queue(4);
    std::atomic<bool> woke{false};
    std::thread waiter([&] {
        const auto item = queue.pop_for(std::chrono::seconds(5));
        EXPECT_FALSE(item);
        woke.store(true);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    queue.close();
    waiter.join();
    EXPECT_TRUE(woke.load());
}

TEST(BoundedQueueTest, PushFailsAfterCloseButPopsStillDrain) {
    BoundedQueue<int> queue(4);
    EXPECT_TRUE(queue.try_push(1));
    queue.close();
    EXPECT_FALSE(queue.try_push(2));
    const auto item = queue.try_pop();
    ASSERT_TRUE(item);
    EXPECT_EQ(*item, 1);
    EXPECT_FALSE(queue.try_pop());
}

TEST(BoundedQueueTest, ManyProducersPreserveEveryItemAndPerProducerOrder) {
    constexpr int kProducers = 4;
    constexpr int kItemsPerProducer = 25'000;
    BoundedQueue<std::pair<int, int>> queue(1'000);

    std::vector<std::thread> producers;
    for (int p = 0; p < kProducers; ++p) {
        producers.emplace_back([&queue, p] {
            for (int i = 0; i < kItemsPerProducer; ++i) {
                while (!queue.try_push({p, i})) {
                    std::this_thread::yield();
                }
            }
        });
    }

    std::vector<std::vector<int>> received(kProducers);
    std::atomic<int> consumed{0};
    const int total = kProducers * kItemsPerProducer;
    std::thread consumer([&] {
        while (consumed.load() < total) {
            const auto item = queue.pop_for(std::chrono::milliseconds(50));
            if (!item) continue;
            received[static_cast<std::size_t>(item->first)].push_back(item->second);
            ++consumed;
        }
    });

    for (auto& producer : producers) producer.join();
    consumer.join();

    for (int p = 0; p < kProducers; ++p) {
        ASSERT_EQ(received[static_cast<std::size_t>(p)].size(),
                  static_cast<std::size_t>(kItemsPerProducer));
        EXPECT_TRUE(std::is_sorted(received[static_cast<std::size_t>(p)].begin(),
                                   received[static_cast<std::size_t>(p)].end()));
    }
}
