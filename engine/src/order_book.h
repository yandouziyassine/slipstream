#pragma once

#include <cstddef>
#include <functional>
#include <map>
#include <optional>
#include <vector>

#include "types.h"

namespace slipstream {

class OrderBook {
public:
    explicit OrderBook(std::size_t max_depth = 10);

    // Both return false and leave the book unchanged if any level is invalid.
    bool apply_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks);
    bool apply_update(const std::vector<Level>& bids, const std::vector<Level>& asks);

    std::optional<Level> best_bid() const;
    std::optional<Level> best_ask() const;
    std::optional<double> mid() const;

    // Levels a taker on `taker_side` would consume, best price first.
    std::vector<Level> liquidity_for(Side taker_side) const;

    bool empty() const;

    // Finite, positive prices and finite, non-negative quantities; a snapshot also needs qty > 0.
    static bool valid_levels(const std::vector<Level>& levels, bool allow_zero_qty);

private:
    void truncate();

    std::size_t max_depth_;
    std::map<double, double, std::greater<>> bids_;
    std::map<double, double> asks_;
};

}  // namespace slipstream
