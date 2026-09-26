#include "almgren_chriss.h"

#include <cmath>

namespace slipstream {
namespace {

constexpr double kLinearThreshold = 1e-9;

bool positive_finite(double value) { return std::isfinite(value) && value > 0.0; }

}  // namespace

std::optional<AlmgrenChrissSchedule> AlmgrenChrissSchedule::create(const SliceParams& params,
                                                                   double sigma, double eta,
                                                                   double risk_aversion) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (!positive_finite(sigma) || !positive_finite(eta) || !positive_finite(risk_aversion)) {
        return std::nullopt;
    }
    const double tau_s = static_cast<double>(params.duration_ns / params.num_slices) / 1e9;
    const double cosh_kappa_tau = 1.0 + risk_aversion * sigma * sigma * tau_s * tau_s / (2.0 * eta);
    if (!std::isfinite(cosh_kappa_tau)) return std::nullopt;
    const double kappa = std::acosh(cosh_kappa_tau) / tau_s;
    if (!std::isfinite(kappa)) return std::nullopt;
    return AlmgrenChrissSchedule(params, kappa, tau_s);
}

AlmgrenChrissSchedule::AlmgrenChrissSchedule(const SliceParams& params, double kappa,
                                             double tau_s)
    : Schedule(params),
      kappa_(kappa),
      tau_s_(tau_s),
      horizon_s_(tau_s * static_cast<double>(params.num_slices)) {}

// x(t)/X = sinh(k(T-t))/sinh(kT), rewritten with only non-positive exponents so it cannot overflow.
double AlmgrenChrissSchedule::remaining_fraction(double t_s) const {
    if (kappa_horizon() < kLinearThreshold) return 1.0 - t_s / horizon_s_;
    const double numerator = std::exp(-kappa_ * t_s) - std::exp(-kappa_ * (2.0 * horizon_s_ - t_s));
    const double denominator = 1.0 - std::exp(-2.0 * kappa_ * horizon_s_);
    return numerator / denominator;
}

double AlmgrenChrissSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const auto& params = slice_params();
    const std::int64_t released = released_slices(params, now_ns);
    if (released == 0) return 0.0;
    if (released == params.num_slices) return params.total_qty;
    const double t_s = static_cast<double>(released) * tau_s_;
    return params.total_qty * (1.0 - remaining_fraction(t_s));
}

}  // namespace slipstream
