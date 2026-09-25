#include "config.h"

#include <gtest/gtest.h>

#include <string>

using slipstream::parse_args;

TEST(Config, DefaultsAreSafe) {
    const auto result = parse_args({});
    ASSERT_TRUE(result.config);
    EXPECT_EQ(result.config->listen_address, "127.0.0.1:50051");
    EXPECT_EQ(result.config->symbol, "BTC/USD");
    EXPECT_DOUBLE_EQ(result.config->limits.max_order_notional, 5000.0);
    EXPECT_DOUBLE_EQ(result.config->limits.max_abs_position, 0.1);
    EXPECT_EQ(result.config->book_depth, 10u);
}

TEST(Config, ParsesAllFlags) {
    const auto result = parse_args({"--listen", "localhost:6000", "--symbol", "ETH/USD",
                                    "--max-order-notional", "250.5", "--max-position", "2",
                                    "--book-depth", "25"});
    ASSERT_TRUE(result.config) << result.error;
    EXPECT_EQ(result.config->listen_address, "localhost:6000");
    EXPECT_EQ(result.config->symbol, "ETH/USD");
    EXPECT_DOUBLE_EQ(result.config->limits.max_order_notional, 250.5);
    EXPECT_DOUBLE_EQ(result.config->limits.max_abs_position, 2.0);
    EXPECT_EQ(result.config->book_depth, 25u);
}

TEST(Config, AllowsEphemeralLoopbackPort) {
    EXPECT_TRUE(parse_args({"--listen", "127.0.0.1:0"}).config);
}

TEST(Config, RejectsUnknownFlag) {
    const auto result = parse_args({"--live", "true"});
    EXPECT_FALSE(result.config);
    EXPECT_NE(result.error.find("unknown flag"), std::string::npos);
}

TEST(Config, RejectsMissingValue) {
    const auto result = parse_args({"--symbol"});
    EXPECT_FALSE(result.config);
    EXPECT_NE(result.error.find("missing value"), std::string::npos);
}

TEST(Config, RejectsNonLoopbackListen) {
    for (const char* addr : {"0.0.0.0:50051", "10.0.0.1:50051", "127.0.0.1", "127.0.0.1:99999",
                             "127.0.0.1:abc"}) {
        EXPECT_FALSE(parse_args({"--listen", addr}).config) << addr;
    }
}

TEST(Config, RejectsBadLimits) {
    for (const char* value : {"abc", "-1", "0", "nan", "inf", "1e400", "5x"}) {
        EXPECT_FALSE(parse_args({"--max-order-notional", value}).config) << value;
        EXPECT_FALSE(parse_args({"--max-position", value}).config) << value;
    }
}

TEST(Config, RejectsBadBookDepth) {
    for (const char* value : {"0", "1001", "-1", "ten", "1\xB2"}) {
        EXPECT_FALSE(parse_args({"--book-depth", value}).config) << value;
    }
}

TEST(Config, RejectsBadSymbol) {
    for (const auto& value : {std::string("btc/usd"), std::string(""), std::string("BTC USD"),
                              std::string(33, 'A'), std::string("BTC/\xC3\x89UR")}) {
        EXPECT_FALSE(parse_args({"--symbol", value}).config) << value;
    }
}

TEST(Config, DefaultsToSingleFeeFreeKraken) {
    const auto result = parse_args({});
    ASSERT_TRUE(result.config);
    ASSERT_EQ(result.config->venues.size(), 1u);
    EXPECT_EQ(result.config->venues[0].name, "kraken");
    EXPECT_DOUBLE_EQ(result.config->venues[0].fee_bps, 0.0);
    EXPECT_EQ(result.config->stale_ns, 2'000'000'000);
}

TEST(Config, ParsesVenuesInOrderAndStaleness) {
    const auto result = parse_args({"--venue", "coinbase:fee_bps=60", "--venue", "kraken:fee_bps=40",
                                    "--stale-ms", "500"});
    ASSERT_TRUE(result.config) << result.error;
    ASSERT_EQ(result.config->venues.size(), 2u);
    EXPECT_EQ(result.config->venues[0].name, "coinbase");
    EXPECT_DOUBLE_EQ(result.config->venues[0].fee_bps, 60.0);
    EXPECT_EQ(result.config->venues[1].name, "kraken");
    EXPECT_DOUBLE_EQ(result.config->venues[1].fee_bps, 40.0);
    EXPECT_EQ(result.config->stale_ns, 500'000'000);
}

TEST(Config, RejectsBadVenues) {
    for (const char* value : {"binance:fee_bps=10", "kraken", "kraken:fee=10", "kraken:fee_bps=",
                              "kraken:fee_bps=-1", "kraken:fee_bps=1001", "kraken:fee_bps=nan",
                              "kraken:fee_bps=10x", ":fee_bps=10", "kraken:fee_bps= 50",
                              "kraken:fee_bps=+5", "kraken:fee_bps=5e2", "kraken:fee_bps=1.2.3",
                              "kraken:fee_bps=."}) {
        EXPECT_FALSE(parse_args({"--venue", value}).config) << value;
    }
    EXPECT_FALSE(parse_args({"--venue", "kraken:fee_bps=1", "--venue", "kraken:fee_bps=2"}).config);
}

TEST(Config, RejectsBadStaleness) {
    for (const char* value : {"0", "-1", "abc", "600001", "1.5"}) {
        EXPECT_FALSE(parse_args({"--stale-ms", value}).config) << value;
    }
}
