#include "risk.h"

#include <cmath>

namespace slipstream {
namespace {

constexpr double kTolerance = 1e-9;

bool positive_finite(double value) { return std::isfinite(value) && value > 0.0; }

bool within(double value, double limit) { return value <= limit * (1.0 + kTolerance); }

}  // namespace

RiskCheck::RiskCheck(RiskLimits limits) : limits_(limits) {}

RiskDecision RiskCheck::check_position(Side side, double qty, double base_position) const {
    const double after = base_position + signed_qty(side, qty);
    if (!within(std::fabs(after), limits_.max_abs_position)) {
        return {false, "position limit exceeded"};
    }
    return {true, ""};
}

RiskDecision RiskCheck::check_parent(Side side, double qty, double ref_price,
                                     double projected_position) const {
    if (!positive_finite(qty)) return {false, "invalid quantity"};
    if (!positive_finite(ref_price)) return {false, "no reference price"};
    if (!within(qty * ref_price, limits_.max_order_notional)) {
        return {false, "order notional limit exceeded"};
    }
    return check_position(side, qty, projected_position);
}

RiskDecision RiskCheck::check_child(Side side, double qty, double fill_cost,
                                    double current_position, double spent_cost) const {
    if (!positive_finite(qty)) return {false, "invalid quantity"};
    if (!positive_finite(fill_cost)) return {false, "invalid fill cost"};
    if (!within(spent_cost + fill_cost, limits_.max_order_notional)) {
        return {false, "order notional limit exceeded"};
    }
    return check_position(side, qty, current_position);
}

}  // namespace slipstream
