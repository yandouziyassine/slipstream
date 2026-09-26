#include "engine.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <utility>

#include "ascii.h"
#include "schedule_factory.h"

namespace slipstream {
namespace {

constexpr double kDustFraction = 1e-9;

bool valid_order_id(const std::string& id) {
    if (id.empty() || id.size() > Engine::kMaxOrderIdLength) return false;
    return std::all_of(id.begin(), id.end(), [](char c) {
        return is_ascii_alnum(c) || c == '-' || c == '_';
    });
}

// Positive means the execution cost money relative to the reference price.
double cost_bps(Side side, double avg_price, double reference) {
    if (avg_price <= 0.0 || reference <= 0.0) return 0.0;
    const double diff = side == Side::Buy ? avg_price - reference : reference - avg_price;
    return diff / reference * 1e4;
}

}  // namespace

Engine::Engine(RiskLimits limits, std::size_t book_depth)
    : Engine(limits, book_depth, {{"kraken", 0.0}}, 0) {}

Engine::Engine(RiskLimits limits, std::size_t book_depth, std::vector<VenueSettings> venues,
              std::int64_t stale_ns)
    : stale_ns_(stale_ns), risk_(limits) {
    venues_.reserve(venues.size());
    for (auto& venue : venues) {
        const double fee_rate = venue.fee_bps / 1e4;
        venues_.push_back(Venue{std::move(venue), fee_rate, OrderBook(book_depth), 0});
    }
}

bool Engine::apply_book_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    return apply_book_snapshot(0, bids, asks, 0);
}

bool Engine::apply_book_update(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    return apply_book_update(0, bids, asks, 0);
}

bool Engine::apply_book_snapshot(std::size_t venue, const std::vector<Level>& bids,
                                 const std::vector<Level>& asks, std::int64_t recv_ns) {
    std::lock_guard lock(mu_);
    if (venue >= venues_.size() || recv_ns < 0) return false;
    if (!venues_[venue].book.apply_snapshot(bids, asks)) return false;
    venues_[venue].last_update_ns = std::max(venues_[venue].last_update_ns, recv_ns);
    latest_ns_ = std::max(latest_ns_, recv_ns);
    return true;
}

bool Engine::apply_book_update(std::size_t venue, const std::vector<Level>& bids,
                               const std::vector<Level>& asks, std::int64_t recv_ns) {
    std::lock_guard lock(mu_);
    if (venue >= venues_.size() || recv_ns < 0) return false;
    if (!venues_[venue].book.apply_update(bids, asks)) return false;
    venues_[venue].last_update_ns = std::max(venues_[venue].last_update_ns, recv_ns);
    latest_ns_ = std::max(latest_ns_, recv_ns);
    return true;
}

bool Engine::apply_trades(const std::vector<Trade>& trades) {
    std::lock_guard lock(mu_);
    double added = 0.0;
    for (const auto& trade : trades) {
        if (!std::isfinite(trade.price) || !std::isfinite(trade.qty)) return false;
        if (trade.price <= 0.0 || trade.qty <= 0.0) return false;
        added += trade.qty;
    }
    if (!std::isfinite(market_volume_ + added)) return false;
    market_volume_ += added;
    return true;
}

double Engine::projected_position_locked() const {
    double projected = position_;
    for (const auto& order : orders_) {
        if (order.state != OrderState::Working) continue;
        projected += signed_qty(order.request.side, order.request.qty - order.filled_qty);
    }
    return projected;
}

bool Engine::fresh_locked(std::size_t venue, std::int64_t now_ns) const {
    if (venues_.size() < 2) return true;
    // The engine's clock never moves backwards, so an old or negative caller time cannot make a
    // stale book fresh again. latest_ns_ >= last_update_ns >= 0, so the subtraction cannot overflow.
    const std::int64_t effective_now = std::max(now_ns, latest_ns_);
    return effective_now - venues_[venue].last_update_ns <= stale_ns_;
}

std::optional<double> Engine::consolidated_mid_locked(std::int64_t now_ns) const {
    std::optional<double> best_bid;
    std::optional<double> best_ask;
    std::optional<double> best_effective_bid;
    std::optional<double> best_effective_ask;
    for (std::size_t v = 0; v < venues_.size(); ++v) {
        if (!fresh_locked(v, now_ns)) continue;
        const auto bid = venues_[v].book.best_bid();
        const auto ask = venues_[v].book.best_ask();
        // A venue's matching engine never rests a crossed book, so one here means bad data.
        if (bid && ask && bid->price >= ask->price) return std::nullopt;
        const double fee_rate = venues_[v].fee_rate;
        if (bid) {
            if (!best_bid || bid->price > *best_bid) best_bid = bid->price;
            const double effective = bid->price * (1.0 - fee_rate);
            if (!best_effective_bid || effective > *best_effective_bid) {
                best_effective_bid = effective;
            }
        }
        if (ask) {
            if (!best_ask || ask->price < *best_ask) best_ask = ask->price;
            const double effective = ask->price * (1.0 + fee_rate);
            if (!best_effective_ask || effective < *best_effective_ask) {
                best_effective_ask = effective;
            }
        }
    }
    if (!best_bid || !best_ask) return std::nullopt;
    // Separate venues are routinely crossed by a little, because taker fees make the cross
    // unprofitable to trade. A cross that survives fees is an arbitrage real venues do not leave
    // standing, so treat it as bad data.
    if (*best_effective_bid >= *best_effective_ask) return std::nullopt;
    return (*best_bid + *best_ask) / 2.0;
}

std::vector<VenueLiquidity> Engine::liquidity_locked(Side side, std::int64_t now_ns,
                                                     std::optional<std::size_t> only) const {
    std::vector<VenueLiquidity> out;
    for (std::size_t v = 0; v < venues_.size(); ++v) {
        if (only && *only != v) continue;
        if (!fresh_locked(v, now_ns)) continue;
        const auto& settings = venues_[v].settings;
        out.push_back({v, venues_[v].fee_rate, venues_[v].book.liquidity_for(side), settings.min_qty,
                       settings.qty_step, settings.min_notional});
    }
    return out;
}

SubmitResult Engine::submit(const ParentOrderRequest& request, const ScheduleSpec& spec) {
    std::lock_guard lock(mu_);
    if (!valid_order_id(request.order_id)) return {false, "invalid order id"};
    if (orders_.size() >= kMaxOrders) return {false, "order capacity reached"};
    const bool duplicate = std::any_of(orders_.begin(), orders_.end(), [&](const ParentOrder& o) {
        return o.request.order_id == request.order_id;
    });
    if (duplicate) return {false, "duplicate order id"};

    auto schedule = make_schedule(
        {request.qty, request.start_ns, request.duration_ns, request.num_slices}, spec,
        MarketState{market_volume_});
    if (!schedule) return {false, "invalid schedule"};

    const auto arrival_mid = consolidated_mid_locked(request.start_ns);
    if (!arrival_mid) return {false, "no market data"};

    const auto decision =
        risk_.check_parent(request.side, request.qty, *arrival_mid, projected_position_locked());
    if (!decision.ok) return {false, decision.reason};

    const auto immediate =
        route(request.side, request.qty, liquidity_locked(request.side, request.start_ns, std::nullopt));
    const double immediate_avg =
        immediate.filled_qty > 0.0 ? immediate.gross_notional / immediate.filled_qty : 0.0;

    orders_.push_back(ParentOrder{request, std::move(schedule), OrderState::Working, 0.0, 0.0, 0.0,
                                  *arrival_mid, cost_bps(request.side, immediate_avg, *arrival_mid),
                                  "", std::vector<double>(venues_.size(), 0.0),
                                  std::vector<char>(venues_.size(), 1)});
    return {true, ""};
}

std::vector<Fill> Engine::step(std::int64_t now_ns) {
    std::lock_guard lock(mu_);
    latest_ns_ = std::max(latest_ns_, now_ns);
    std::vector<Fill> fills;
    const auto ref_price = consolidated_mid_locked(now_ns);
    const MarketState market{market_volume_};
    for (auto& order : orders_) {
        if (order.state != OrderState::Working) continue;
        advance_locked(order, now_ns, ref_price, market, fills);
        if (order.state == OrderState::Working && order.schedule->expired(now_ns)) {
            order.state = OrderState::Halted;
            order.halt_reason = "deadline reached";
        }
    }
    return fills;
}

void Engine::advance_locked(ParentOrder& order, std::int64_t now_ns,
                            std::optional<double> ref_price, const MarketState& market,
                            std::vector<Fill>& fills) {
    const double dust = order.request.qty * kDustFraction;
    const double child = order.schedule->target_qty_at(now_ns, market) - order.filled_qty;
    if (child <= dust || !ref_price) return;

    const auto decision = risk_.check_child(order.request.side, child, *ref_price, position_,
                                            order.filled_notional);
    if (!decision.ok) {
        order.state = OrderState::Halted;
        order.halt_reason = decision.reason;
        return;
    }

    const auto result =
        route(order.request.side, child, liquidity_locked(order.request.side, now_ns, std::nullopt));
    if (result.filled_qty <= 0.0) return;

    for (const auto& leg : result.legs) {
        fills.push_back({order.request.order_id, now_ns, leg.qty, leg.gross_notional / leg.qty,
                         venues_[leg.venue].settings.name, leg.fee});
    }

    order.filled_qty += result.filled_qty;
    order.filled_notional += result.gross_notional;
    order.fees += result.fees;
    position_ += signed_qty(order.request.side, result.filled_qty);

    for (std::size_t v = 0; v < venues_.size(); ++v) {
        if (!order.venue_available[v]) continue;
        if (!fresh_locked(v, now_ns)) {
            order.venue_available[v] = 0;
            continue;
        }
        const auto alone = route(order.request.side, result.filled_qty,
                                 liquidity_locked(order.request.side, now_ns, v));
        if (alone.filled_qty < result.filled_qty * (1.0 - 1e-9)) {
            order.venue_available[v] = 0;
        } else {
            order.venue_all_in_notional[v] += all_in_notional(order.request.side, alone);
        }
    }

    if (order.request.qty - order.filled_qty <= dust) order.state = OrderState::Completed;
}

std::vector<OrderStatus> Engine::statuses() const {
    std::lock_guard lock(mu_);
    std::vector<OrderStatus> out;
    out.reserve(orders_.size());
    for (const auto& order : orders_) {
        const double avg = order.filled_qty > 0.0 ? order.filled_notional / order.filled_qty : 0.0;
        const double fees_bps =
            order.filled_notional > 0.0 ? order.fees / order.filled_notional * 1e4 : 0.0;
        const double all_in_avg =
            order.filled_qty > 0.0
                ? (order.request.side == Side::Buy ? order.filled_notional + order.fees
                                                   : order.filled_notional - order.fees) /
                      order.filled_qty
                : 0.0;
        const double routed_all_in_bps = cost_bps(order.request.side, all_in_avg, order.arrival_mid);

        std::vector<VenueCost> venue_costs;
        venue_costs.reserve(venues_.size());
        for (std::size_t v = 0; v < venues_.size(); ++v) {
            const bool available = order.venue_available[v] != 0 && order.filled_qty > 0.0;
            const double all_in_bps =
                available ? cost_bps(order.request.side,
                                     order.venue_all_in_notional[v] / order.filled_qty,
                                     order.arrival_mid)
                          : 0.0;
            venue_costs.push_back({venues_[v].settings.name, all_in_bps, available});
        }

        out.push_back({order.request.order_id, order.request.side, order.state, order.request.qty,
                       order.filled_qty, avg, order.arrival_mid,
                       cost_bps(order.request.side, avg, order.arrival_mid),
                       order.immediate_cost_bps, order.halt_reason, order.schedule->name(),
                       order.fees, fees_bps, routed_all_in_bps, std::move(venue_costs)});
    }
    return out;
}

double Engine::position() const {
    std::lock_guard lock(mu_);
    return position_;
}

std::optional<double> Engine::mid() const {
    std::lock_guard lock(mu_);
    return consolidated_mid_locked(latest_ns_);
}

double Engine::market_volume() const {
    std::lock_guard lock(mu_);
    return market_volume_;
}

std::optional<std::size_t> Engine::venue_index(std::string_view name) const {
    std::lock_guard lock(mu_);
    for (std::size_t v = 0; v < venues_.size(); ++v) {
        if (std::string_view(venues_[v].settings.name) == name) return v;
    }
    return std::nullopt;
}

std::size_t Engine::venue_count() const {
    std::lock_guard lock(mu_);
    return venues_.size();
}

std::vector<VenueSettings> Engine::venue_settings() const {
    std::lock_guard lock(mu_);
    std::vector<VenueSettings> out;
    out.reserve(venues_.size());
    for (const auto& venue : venues_) out.push_back(venue.settings);
    return out;
}

}  // namespace slipstream
