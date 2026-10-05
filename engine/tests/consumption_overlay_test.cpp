#include "consumption_overlay.h"

#include <gtest/gtest.h>

#include <vector>

using namespace slipstream;

namespace {

const std::vector<BookLevel> kAsks{{100.0, 1.0, 7}, {101.0, 2.0, 3}, {102.0, 5.0, 9}};

void expect_levels(const std::vector<Level>& actual, const std::vector<Level>& expected) {
    ASSERT_EQ(actual.size(), expected.size());
    for (std::size_t i = 0; i < expected.size(); ++i) {
        EXPECT_DOUBLE_EQ(actual[i].price, expected[i].price) << i;
        EXPECT_DOUBLE_EQ(actual[i].qty, expected[i].qty) << i;
    }
}

}  // namespace

TEST(ConsumptionOverlay, NothingTakenLeavesTheDisplayedBook) {
    const ConsumptionOverlay overlay;
    expect_levels(overlay.remaining(kAsks), {{100.0, 1.0}, {101.0, 2.0}, {102.0, 5.0}});
    EXPECT_EQ(overlay.size(), 0u);
}

TEST(ConsumptionOverlay, PartialTakeReducesTheLevel) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {0.4});
    expect_levels(overlay.remaining(kAsks), {{100.0, 0.6}, {101.0, 2.0}, {102.0, 5.0}});
    overlay.record(kAsks, {0.1});
    expect_levels(overlay.remaining(kAsks), {{100.0, 0.5}, {101.0, 2.0}, {102.0, 5.0}});
}

TEST(ConsumptionOverlay, FullTakeHidesTheLevelAndLaterTakesAlignWithWhatIsLeft) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {1.0, 0.5});
    expect_levels(overlay.remaining(kAsks), {{101.0, 1.5}, {102.0, 5.0}});
    // Index 0 of what this order can still see is 101, not the hidden 100.
    overlay.record(kAsks, {1.5, 1.0});
    expect_levels(overlay.remaining(kAsks), {{102.0, 4.0}});
}

TEST(ConsumptionOverlay, TakeLargerThanDisplayedFloorsAtZero) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {1.5});
    expect_levels(overlay.remaining(kAsks), {{101.0, 2.0}, {102.0, 5.0}});
}

TEST(ConsumptionOverlay, NewVersionOfALevelResetsWhatWasTaken) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {1.0, 0.5});
    const std::vector<BookLevel> refreshed{{100.0, 1.0, 10}, {101.0, 2.0, 3}, {102.0, 5.0, 9}};
    expect_levels(overlay.remaining(refreshed), {{100.0, 1.0}, {101.0, 1.5}, {102.0, 5.0}});
}

TEST(ConsumptionOverlay, RecordDropsEntriesForLevelsNoLongerInTheBook) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {1.0, 2.0, 1.0});
    EXPECT_EQ(overlay.size(), 3u);
    const std::vector<BookLevel> refreshed{{100.0, 1.0, 10}, {102.0, 5.0, 9}};
    overlay.record(refreshed, {0.5});
    EXPECT_EQ(overlay.size(), 2u);
    expect_levels(overlay.remaining(refreshed), {{100.0, 0.5}, {102.0, 4.0}});
}

TEST(ConsumptionOverlay, EntriesNeverExceedTheLevelsInTheBook) {
    ConsumptionOverlay overlay;
    for (std::uint64_t round = 0; round < 100; ++round) {
        const std::vector<BookLevel> book{{100.0, 1.0, 3 * round + 1},
                                          {101.0, 1.0, 3 * round + 2},
                                          {102.0, 1.0, 3 * round + 3}};
        overlay.record(book, {1.0, 0.5});
        EXPECT_LE(overlay.size(), book.size());
    }
}

TEST(ConsumptionOverlay, ClearForgetsEverything) {
    ConsumptionOverlay overlay;
    overlay.record(kAsks, {1.0, 0.5});
    overlay.clear();
    EXPECT_EQ(overlay.size(), 0u);
    expect_levels(overlay.remaining(kAsks), {{100.0, 1.0}, {101.0, 2.0}, {102.0, 5.0}});
}

TEST(ConsumptionOverlay, TakesThatSumToTheLevelUpToRoundingHideIt) {
    // 0.7 + 0.2 + 0.1 is 0.9999999999999999 in doubles: no rounding dust may stay visible.
    const std::vector<BookLevel> book{{100.0, 1.0, 1}, {101.0, 1.0, 2}};
    ConsumptionOverlay overlay;
    overlay.record(book, {0.7});
    overlay.record(book, {0.2});
    overlay.record(book, {0.1});
    expect_levels(overlay.remaining(book), {{101.0, 1.0}});
}
