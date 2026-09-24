#include "order_book.h"

#include <cmath>
#include <iterator>

namespace slipstream {

OrderBook::OrderBook(std::size_t max_depth) : max_depth_(max_depth) {}

bool OrderBook::valid_levels(const std::vector<Level>& levels, bool allow_zero_qty) {
    for (const auto& level : levels) {
        if (!std::isfinite(level.price) || !std::isfinite(level.qty)) return false;
        if (level.price <= 0.0 || level.qty < 0.0) return false;
        if (!allow_zero_qty && level.qty == 0.0) return false;
    }
    return true;
}

bool OrderBook::apply_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    if (!valid_levels(bids, false) || !valid_levels(asks, false)) return false;
    bids_.clear();
    asks_.clear();
    for (const auto& level : bids) bids_[level.price] = level.qty;
    for (const auto& level : asks) asks_[level.price] = level.qty;
    truncate();
    return true;
}

bool OrderBook::apply_update(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    if (!valid_levels(bids, true) || !valid_levels(asks, true)) return false;
    auto apply = [](auto& side, const std::vector<Level>& levels) {
        for (const auto& level : levels) {
            if (level.qty == 0.0) {
                side.erase(level.price);
            } else {
                side[level.price] = level.qty;
            }
        }
    };
    apply(bids_, bids);
    apply(asks_, asks);
    truncate();
    return true;
}

void OrderBook::truncate() {
    while (bids_.size() > max_depth_) bids_.erase(std::prev(bids_.end()));
    while (asks_.size() > max_depth_) asks_.erase(std::prev(asks_.end()));
}

std::optional<Level> OrderBook::best_bid() const {
    if (bids_.empty()) return std::nullopt;
    return Level{bids_.begin()->first, bids_.begin()->second};
}

std::optional<Level> OrderBook::best_ask() const {
    if (asks_.empty()) return std::nullopt;
    return Level{asks_.begin()->first, asks_.begin()->second};
}

std::optional<double> OrderBook::mid() const {
    const auto bid = best_bid();
    const auto ask = best_ask();
    if (!bid || !ask) return std::nullopt;
    return (bid->price + ask->price) / 2.0;
}

std::vector<Level> OrderBook::liquidity_for(Side taker_side) const {
    std::vector<Level> levels;
    if (taker_side == Side::Buy) {
        for (const auto& [price, qty] : asks_) levels.push_back({price, qty});
    } else {
        for (const auto& [price, qty] : bids_) levels.push_back({price, qty});
    }
    return levels;
}

bool OrderBook::empty() const { return bids_.empty() && asks_.empty(); }

}  // namespace slipstream
