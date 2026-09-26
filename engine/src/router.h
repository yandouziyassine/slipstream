#pragma once

#include <cstddef>
#include <vector>

#include "types.h"

namespace slipstream {

struct VenueLiquidity {
    std::size_t venue;
    double fee_rate;
    std::vector<Level> levels;  // best-first, gross prices
    double min_qty = 0.0;
    double qty_step = 0.0;  // 0 means no rounding
    double min_notional = 0.0;
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
// Ties go to the venue listed first. Each leg is then floored to its venue's qty_step (the excess
// comes off its worst prices) and dropped if below min_qty or min_notional. Dropped quantity is
// not re-routed.
RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues);

// Buy: gross + fees paid. Sell: gross - fees paid.
double all_in_notional(Side side, const RouteResult& result);

}  // namespace slipstream
