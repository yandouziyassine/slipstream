#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <vector>

#include "types.h"

namespace slipstream {

// A level plus the version the book gave it when the feed last set it. Versions are unique within
// one book, across both sides, so a new version means the feed has refreshed that level.
struct BookLevel {
    double price;
    double qty;
    std::uint64_t version;
};

class OrderBook {
public:
    explicit OrderBook(std::size_t max_depth = 10);

    // Both return false and leave the book unchanged if any level is invalid.
    // Every level a snapshot writes, or an update sets, gets a new version.
    bool apply_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks);
    bool apply_update(const std::vector<Level>& bids, const std::vector<Level>& asks);

    std::optional<Level> best_bid() const;
    std::optional<Level> best_ask() const;
    std::optional<double> mid() const;

    // Levels a taker on `taker_side` would consume, best price first.
    std::vector<Level> liquidity_for(Side taker_side) const;
    std::vector<BookLevel> versioned_liquidity_for(Side taker_side) const;

    bool empty() const;

    // Finite, positive prices and finite, non-negative quantities; a snapshot also needs qty > 0.
    static bool valid_levels(const std::vector<Level>& levels, bool allow_zero_qty);

private:
    struct Slot {
        double qty;
        std::uint64_t version;
    };

    void truncate();

    std::size_t max_depth_;
    std::uint64_t next_version_ = 0;
    std::map<double, Slot, std::greater<>> bids_;
    std::map<double, Slot> asks_;
};

}  // namespace slipstream
