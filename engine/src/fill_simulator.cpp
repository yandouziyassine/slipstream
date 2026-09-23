#include "fill_simulator.h"

#include <algorithm>

namespace slipstream {

FillResult simulate_market_fill(const std::vector<Level>& liquidity, double qty) {
    double remaining = qty;
    double filled = 0.0;
    double notional = 0.0;
    for (const auto& level : liquidity) {
        if (remaining <= 0.0) break;
        const double take = std::min(remaining, level.qty);
        filled += take;
        notional += take * level.price;
        remaining -= take;
    }
    if (filled <= 0.0) return {0.0, 0.0};
    return {filled, notional / filled};
}

}  // namespace slipstream
