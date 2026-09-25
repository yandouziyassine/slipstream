#pragma once

#include <cstddef>
#include <vector>

#include "types.h"

namespace slipstream {

struct VenueLiquidity {
    std::size_t venue;
    double fee_rate;
    std::vector<Level> levels;  // best-first, gross prices
};

struct RouteLeg {
    std::size_t venue;
    double qty;
    double gross_notional;
    double fee;
};

struct RouteResult {
    std::vector<RouteLeg> legs;  // one per venue used, ordered by venue index
    double filled_qty = 0.0;
    double gross_notional = 0.0;
    double fees = 0.0;
};

// Takes the best fee-adjusted prices across venues until qty is filled (paper: books unchanged).
// Ties go to the venue listed first.
RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues);

// Buy: gross + fees paid. Sell: gross - fees paid.
double all_in_notional(Side side, const RouteResult& result);

}  // namespace slipstream
