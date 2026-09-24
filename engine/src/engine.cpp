#include "engine.h"

#include <algorithm>

#include "ascii.h"
#include "fill_simulator.h"

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

Engine::Engine(RiskLimits limits, std::size_t book_depth) : book_(book_depth), risk_(limits) {}

bool Engine::apply_book_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    std::lock_guard lock(mu_);
    return book_.apply_snapshot(bids, asks);
}

bool Engine::apply_book_update(const std::vector<Level>& bids, const std::vector<Level>& asks) {
    std::lock_guard lock(mu_);
    return book_.apply_update(bids, asks);
}

double Engine::projected_position_locked() const {
    double projected = position_;
    for (const auto& order : orders_) {
        if (order.state != OrderState::Working) continue;
        projected += signed_qty(order.request.side, order.request.qty - order.filled_qty);
    }
    return projected;
}

SubmitResult Engine::submit(const ParentOrderRequest& request) {
    std::lock_guard lock(mu_);
    if (!valid_order_id(request.order_id)) return {false, "invalid order id"};
    if (orders_.size() >= kMaxOrders) return {false, "order capacity reached"};
    const bool duplicate = std::any_of(orders_.begin(), orders_.end(), [&](const ParentOrder& o) {
        return o.request.order_id == request.order_id;
    });
    if (duplicate) return {false, "duplicate order id"};

    const auto schedule = TwapSchedule::create(
        {request.qty, request.start_ns, request.duration_ns, request.num_slices});
    if (!schedule) return {false, "invalid schedule"};

    const auto arrival_mid = book_.mid();
    if (!arrival_mid) return {false, "no market data"};

    const auto decision =
        risk_.check_parent(request.side, request.qty, *arrival_mid, projected_position_locked());
    if (!decision.ok) return {false, decision.reason};

    const auto immediate = simulate_market_fill(book_.liquidity_for(request.side), request.qty);
    orders_.push_back(ParentOrder{request, *schedule, OrderState::Working, 0.0, 0.0, *arrival_mid,
                                  cost_bps(request.side, immediate.avg_price, *arrival_mid), ""});
    return {true, ""};
}

std::vector<Fill> Engine::step(std::int64_t now_ns) {
    std::lock_guard lock(mu_);
    std::vector<Fill> fills;
    const auto ref_price = book_.mid();
    for (auto& order : orders_) {
        if (order.state != OrderState::Working) continue;
        const double dust = order.request.qty * kDustFraction;
        const double child = order.schedule.target_qty_at(now_ns, MarketState{}) - order.filled_qty;
        if (child <= dust || !ref_price) continue;

        const auto decision = risk_.check_child(order.request.side, child, *ref_price, position_,
                                                order.filled_notional);
        if (!decision.ok) {
            order.state = OrderState::Halted;
            order.halt_reason = decision.reason;
            continue;
        }

        const auto result = simulate_market_fill(book_.liquidity_for(order.request.side), child);
        if (result.filled_qty <= 0.0) continue;

        order.filled_qty += result.filled_qty;
        order.filled_notional += result.filled_qty * result.avg_price;
        position_ += signed_qty(order.request.side, result.filled_qty);
        fills.push_back({order.request.order_id, now_ns, result.filled_qty, result.avg_price});
        if (order.request.qty - order.filled_qty <= dust) order.state = OrderState::Completed;
    }
    return fills;
}

std::vector<OrderStatus> Engine::statuses() const {
    std::lock_guard lock(mu_);
    std::vector<OrderStatus> out;
    out.reserve(orders_.size());
    for (const auto& order : orders_) {
        const double avg = order.filled_qty > 0.0 ? order.filled_notional / order.filled_qty : 0.0;
        out.push_back({order.request.order_id, order.request.side, order.state, order.request.qty,
                       order.filled_qty, avg, order.arrival_mid,
                       cost_bps(order.request.side, avg, order.arrival_mid),
                       order.immediate_cost_bps, order.halt_reason});
    }
    return out;
}

double Engine::position() const {
    std::lock_guard lock(mu_);
    return position_;
}

std::optional<double> Engine::mid() const {
    std::lock_guard lock(mu_);
    return book_.mid();
}

}  // namespace slipstream
