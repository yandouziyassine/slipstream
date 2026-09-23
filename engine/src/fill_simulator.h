#pragma once

#include <vector>

#include "types.h"

namespace slipstream {

struct FillResult {
    double filled_qty;
    double avg_price;
};

// Takes liquidity best-first. Paper mode: the book itself is not modified.
FillResult simulate_market_fill(const std::vector<Level>& liquidity, double qty);

}  // namespace slipstream
