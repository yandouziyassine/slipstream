#include "order_book.h"

#include <gtest/gtest.h>

#include <limits>

using slipstream::OrderBook;
using slipstream::Side;

TEST(OrderBook, EmptyBookHasNoPrices) {
    OrderBook book;
    EXPECT_TRUE(book.empty());
    EXPECT_FALSE(book.best_bid().has_value());
    EXPECT_FALSE(book.best_ask().has_value());
    EXPECT_FALSE(book.mid().has_value());
}

TEST(OrderBook, SnapshotSetsBestPricesAndMid) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}, {99.0, 2.0}}, {{101.0, 1.5}, {102.0, 3.0}}));
    EXPECT_DOUBLE_EQ(book.best_bid()->price, 100.0);
    EXPECT_DOUBLE_EQ(book.best_ask()->price, 101.0);
    EXPECT_DOUBLE_EQ(*book.mid(), 100.5);
}

TEST(OrderBook, SnapshotReplacesPreviousState) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}}, {{101.0, 1.0}}));
    ASSERT_TRUE(book.apply_snapshot({{90.0, 1.0}}, {{91.0, 1.0}}));
    EXPECT_DOUBLE_EQ(book.best_bid()->price, 90.0);
    EXPECT_EQ(book.liquidity_for(Side::Buy).size(), 1u);
}

TEST(OrderBook, UpdateWithZeroQtyRemovesLevel) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}, {99.0, 2.0}}, {{101.0, 1.0}}));
    ASSERT_TRUE(book.apply_update({{100.0, 0.0}}, {}));
    EXPECT_DOUBLE_EQ(book.best_bid()->price, 99.0);
}

TEST(OrderBook, UpdateInsertsAndModifiesLevels) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}}, {{101.0, 1.0}}));
    ASSERT_TRUE(book.apply_update({{100.5, 2.0}}, {{101.0, 4.0}}));
    EXPECT_DOUBLE_EQ(book.best_bid()->price, 100.5);
    EXPECT_DOUBLE_EQ(book.best_ask()->qty, 4.0);
}

TEST(OrderBook, InvalidLevelsAreRejectedWithoutMutation) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}}, {{101.0, 1.0}}));
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    EXPECT_FALSE(book.apply_update({{nan, 1.0}}, {}));
    EXPECT_FALSE(book.apply_update({{100.0, inf}}, {}));
    EXPECT_FALSE(book.apply_update({{-1.0, 1.0}}, {}));
    EXPECT_FALSE(book.apply_update({}, {{101.0, -1.0}}));
    EXPECT_FALSE(book.apply_snapshot({{100.0, 0.0}}, {}));
    EXPECT_DOUBLE_EQ(book.best_bid()->price, 100.0);
    EXPECT_DOUBLE_EQ(book.best_ask()->price, 101.0);
}

TEST(OrderBook, TruncatesToMaxDepthKeepingBestLevels) {
    OrderBook book(2);
    ASSERT_TRUE(book.apply_snapshot({{100.0, 1.0}, {99.0, 1.0}, {98.0, 1.0}},
                                    {{101.0, 1.0}, {102.0, 1.0}, {103.0, 1.0}}));
    const auto asks = book.liquidity_for(Side::Buy);
    const auto bids = book.liquidity_for(Side::Sell);
    ASSERT_EQ(asks.size(), 2u);
    ASSERT_EQ(bids.size(), 2u);
    EXPECT_DOUBLE_EQ(asks.back().price, 102.0);
    EXPECT_DOUBLE_EQ(bids.back().price, 99.0);
}

TEST(OrderBook, LiquidityIsOrderedBestFirst) {
    OrderBook book;
    ASSERT_TRUE(book.apply_snapshot({{99.0, 2.0}, {100.0, 1.0}}, {{102.0, 3.0}, {101.0, 1.5}}));
    const auto asks = book.liquidity_for(Side::Buy);
    EXPECT_DOUBLE_EQ(asks[0].price, 101.0);
    EXPECT_DOUBLE_EQ(asks[1].price, 102.0);
    const auto bids = book.liquidity_for(Side::Sell);
    EXPECT_DOUBLE_EQ(bids[0].price, 100.0);
    EXPECT_DOUBLE_EQ(bids[1].price, 99.0);
}
