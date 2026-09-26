#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "risk.h"

namespace slipstream {

struct VenueConfig {
    std::string name;
    double fee_bps;
    double min_qty = 0.0;
    double qty_step = 0.0;
    double min_notional = 0.0;
};

constexpr std::array<std::string_view, 2> kKnownVenues{"kraken", "coinbase"};

struct EngineConfig {
    std::string listen_address = "127.0.0.1:50051";
    std::string symbol = "BTC/USD";
    RiskLimits limits{5000.0, 0.1};
    std::size_t book_depth = 10;
    std::vector<VenueConfig> venues{{"kraken", 0.0}};
    std::int64_t stale_ns = 2'000'000'000;
    double max_deviation_bps = 50.0;
};

struct ParseResult {
    std::optional<EngineConfig> config;
    std::string error;
};

// args excludes the program name.
ParseResult parse_args(const std::vector<std::string>& args);

}  // namespace slipstream
