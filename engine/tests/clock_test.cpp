#include "clock.h"

#include <gtest/gtest.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>

using slipstream::LiveClock;

TEST(LiveClockTest, NowNsNeverDecreases) {
    LiveClock clock;
    std::int64_t previous = clock.now_ns();
    for (int i = 0; i < 1000; ++i) {
        const std::int64_t current = clock.now_ns();
        EXPECT_GE(current, previous);
        previous = current;
    }
}

TEST(LiveClockTest, NowNsIsWithinOneSecondOfSystemClock) {
    LiveClock clock;
    const std::int64_t system_now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                                            std::chrono::system_clock::now().time_since_epoch())
                                            .count();
    const std::int64_t clock_ns = clock.now_ns();
    constexpr std::int64_t kOneSecondNs = 1'000'000'000;
    EXPECT_LE(std::abs(clock_ns - system_now_ns), kOneSecondNs);
}
