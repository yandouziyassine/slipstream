#include "config.h"

#include <gtest/gtest.h>

#include <string>
#include <utility>

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

TEST(Config, VenueRulesDefaultToZero) {
    const auto result = parse_args({"--venue", "kraken:fee_bps=40"});
    ASSERT_TRUE(result.config) << result.error;
    const auto& venue = result.config->venues.at(0);
    EXPECT_DOUBLE_EQ(venue.min_qty, 0.0);
    EXPECT_DOUBLE_EQ(venue.qty_step, 0.0);
    EXPECT_DOUBLE_EQ(venue.min_notional, 0.0);
}

TEST(Config, ParsesVenueRules) {
    const auto result = parse_args(
        {"--venue", "kraken:fee_bps=40,min_qty=0.00005,qty_step=0.00000001,min_notional=0.5",
         "--venue", "coinbase:fee_bps=60,min_notional=1"});
    ASSERT_TRUE(result.config) << result.error;
    const auto& kraken = result.config->venues.at(0);
    EXPECT_DOUBLE_EQ(kraken.fee_bps, 40.0);
    EXPECT_DOUBLE_EQ(kraken.min_qty, 0.00005);
    EXPECT_DOUBLE_EQ(kraken.qty_step, 0.00000001);
    EXPECT_DOUBLE_EQ(kraken.min_notional, 0.5);
    const auto& coinbase = result.config->venues.at(1);
    EXPECT_DOUBLE_EQ(coinbase.min_qty, 0.0);
    EXPECT_DOUBLE_EQ(coinbase.min_notional, 1.0);
}

TEST(Config, AcceptsVenueRulesAtTheirBounds) {
    const auto result = parse_args({"--venue",
                                    "kraken:fee_bps=1000,min_qty=1000000,qty_step=1000000,"
                                    "min_notional=1000000000"});
    ASSERT_TRUE(result.config) << result.error;
    EXPECT_DOUBLE_EQ(result.config->venues.at(0).min_notional, 1e9);
}

TEST(Config, RejectsBadVenueRules) {
    for (const char* value :
         {"kraken:fee_bps=40,", "kraken:fee_bps=40,,min_qty=1", "kraken:fee_bps=40,min_qty=",
          "kraken:fee_bps=40,min_qty", "kraken:fee_bps=40,lot=1", "kraken:min_qty=1",
          "kraken:fee_bps=40,min_qty=1,min_qty=2", "kraken:fee_bps=1,fee_bps=2",
          "kraken:fee_bps=40,min_qty=1e-5", "kraken:fee_bps=40,min_qty=-1",
          "kraken:fee_bps=40,min_qty=+1", "kraken:fee_bps=40,min_qty= 1",
          "kraken:fee_bps=40,min_qty=1000001", "kraken:fee_bps=40,qty_step=1000001",
          "kraken:fee_bps=40,min_notional=1000000001", "kraken:fee_bps=40,qty_step=nan",
          "kraken:fee_bps=40,min_notional=inf", "kraken:fee_bps=40;min_qty=1",
          "kraken:fee_bps=40,MIN_QTY=1", "kraken:fee_bps=40,min_qty==1"}) {
        EXPECT_FALSE(parse_args({"--venue", value}).config) << value;
    }
}

TEST(Config, MaxDeviationDefaultsTo50Bps) {
    const auto result = parse_args({});
    ASSERT_TRUE(result.config);
    EXPECT_DOUBLE_EQ(result.config->max_deviation_bps, 50.0);
}

TEST(Config, ParsesMaxDeviation) {
    for (const auto& [text, expected] :
         {std::pair{"25.5", 25.5}, std::pair{"10000", 10000.0}, std::pair{"0.01", 0.01}}) {
        const auto result = parse_args({"--max-deviation-bps", text});
        ASSERT_TRUE(result.config) << text << ": " << result.error;
        EXPECT_DOUBLE_EQ(result.config->max_deviation_bps, expected);
    }
}

TEST(Config, RejectsBadMaxDeviation) {
    for (const char* value :
         {"0", "0.0", "10000.1", "-1", "+5", "1e2", "abc", "", "nan", "inf", " 5", "5 ", "."}) {
        EXPECT_FALSE(parse_args({"--max-deviation-bps", value}).config) << value;
    }
}

TEST(Config, RejectsBadStaleness) {
    for (const char* value : {"0", "-1", "abc", "600001", "1.5"}) {
        EXPECT_FALSE(parse_args({"--stale-ms", value}).config) << value;
    }
}

TEST(Config, ClockDefaultsToLive) {
    const auto result = parse_args({});
    ASSERT_TRUE(result.config);
    EXPECT_EQ(result.config->clock, slipstream::ClockMode::Live);
}

TEST(Config, ParsesClockFlag) {
    const auto live = parse_args({"--clock", "live"});
    ASSERT_TRUE(live.config) << live.error;
    EXPECT_EQ(live.config->clock, slipstream::ClockMode::Live);

    const auto replay = parse_args({"--clock", "replay"});
    ASSERT_TRUE(replay.config) << replay.error;
    EXPECT_EQ(replay.config->clock, slipstream::ClockMode::Replay);
}

TEST(Config, RejectsBadClock) {
    for (const char* value : {"Live", "LIVE", "Replay", "replay ", " replay", "sim", "", "0"}) {
        EXPECT_FALSE(parse_args({"--clock", value}).config) << value;
    }
}
