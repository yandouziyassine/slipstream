#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <vector>

#include "risk.h"

namespace slipstream {

struct EngineConfig {
    std::string listen_address = "127.0.0.1:50051";
    std::string symbol = "BTC/USD";
    RiskLimits limits{5000.0, 0.1};
    std::size_t book_depth = 10;
};

struct ParseResult {
    std::optional<EngineConfig> config;
    std::string error;
};

// args excludes the program name.
ParseResult parse_args(const std::vector<std::string>& args);

}  // namespace slipstream
