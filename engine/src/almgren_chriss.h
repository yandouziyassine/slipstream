#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

// Almgren-Chriss optimal liquidation: linear temporary impact (eta), no permanent impact,
// risk aversion lambda. Units: sigma in price/sqrt(s), eta in price*s/qty^2, lambda per price.
class AlmgrenChrissSchedule final : public Schedule {
public:
    static std::optional<AlmgrenChrissSchedule> create(const SliceParams& params, double sigma,
                                                       double eta, double risk_aversion);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "almgren_chriss"; }

    // ~0 behaves like TWAP; larger values front-load execution.
    double kappa_horizon() const { return kappa_ * horizon_s_; }

private:
    AlmgrenChrissSchedule(const SliceParams& params, double kappa, double tau_s);
    double remaining_fraction(double t_s) const;

    double kappa_;
    double tau_s_;
    double horizon_s_;
};

}  // namespace slipstream
