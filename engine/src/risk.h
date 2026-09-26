#pragma once

#include <string>

#include "types.h"

namespace slipstream {

struct RiskLimits {
    double max_order_notional;
    double max_abs_position;
};

struct RiskDecision {
    bool ok;
    std::string reason;
};

class RiskCheck {
public:
    explicit RiskCheck(RiskLimits limits);

    // projected_position includes the remaining qty of every other working order.
    RiskDecision check_parent(Side side, double qty, double ref_price,
                              double projected_position) const;
    // fill_cost is the routed child's gross notional plus fees; spent_cost is the same total for
    // everything this parent order has already filled.
    RiskDecision check_child(Side side, double qty, double fill_cost, double current_position,
                             double spent_cost) const;

    const RiskLimits& limits() const { return limits_; }

private:
    RiskDecision check_position(Side side, double qty, double base_position) const;

    RiskLimits limits_;
};

}  // namespace slipstream
