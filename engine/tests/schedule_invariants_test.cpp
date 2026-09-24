#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <random>
#include <vector>

#include "almgren_chriss.h"
#include "pov.h"
#include "twap.h"
#include "vwap.h"

using namespace slipstream;

namespace {

constexpr std::int64_t kSec = 1'000'000'000;

void expect_monotone_and_bounded(const Schedule& schedule, double total, std::mt19937& rng) {
    std::uniform_int_distribution<std::int64_t> time_dist(-kSec, 30 * kSec);
    std::uniform_real_distribution<double> volume_step(0.0, 5.0);
    std::vector<std::int64_t> times(200);
    for (auto& t : times) t = time_dist(rng);
    std::sort(times.begin(), times.end());
    double volume = 0.0;
    double previous = 0.0;
    for (const auto t : times) {
        volume += volume_step(rng);
        const double target = schedule.target_qty_at(t, MarketState{volume});
        EXPECT_GE(target, previous - 1e-12) << schedule.name();
        EXPECT_GE(target, 0.0) << schedule.name();
        EXPECT_LE(target, total + 1e-12) << schedule.name();
        previous = target;
    }
}

}  // namespace

TEST(ScheduleInvariants, AllSchedulesAreMonotoneAndBounded) {
    std::mt19937 rng(42);
    std::uniform_int_distribution<std::int32_t> slices_dist(1, 50);
    std::uniform_real_distribution<double> positive(0.01, 10.0);
    for (int trial = 0; trial < 50; ++trial) {
        const std::int32_t slices = slices_dist(rng);
        const SliceParams params{positive(rng), 0, 20 * kSec, slices};
        std::vector<double> weights(static_cast<std::size_t>(slices));
        for (auto& w : weights) w = positive(rng);
        const auto twap = TwapSchedule::create(params);
        const auto vwap = VwapSchedule::create(params, weights);
        const auto ac =
            AlmgrenChrissSchedule::create(params, positive(rng), positive(rng), positive(rng));
        const auto pov = PovSchedule::create(params, 0.25, 0.0);
        ASSERT_TRUE(twap && vwap && ac && pov);
        expect_monotone_and_bounded(*twap, params.total_qty, rng);
        expect_monotone_and_bounded(*vwap, params.total_qty, rng);
        expect_monotone_and_bounded(*ac, params.total_qty, rng);
        expect_monotone_and_bounded(*pov, params.total_qty, rng);
    }
}
