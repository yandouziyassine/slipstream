#include "vwap.h"

#include <cmath>
#include <cstddef>
#include <utility>

namespace slipstream {

std::optional<VwapSchedule> VwapSchedule::create(const SliceParams& params,
                                                 const std::vector<double>& weights) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (weights.size() != static_cast<std::size_t>(params.num_slices)) return std::nullopt;
    double total = 0.0;
    for (const double weight : weights) {
        if (!std::isfinite(weight) || weight <= 0.0) return std::nullopt;
        total += weight;
    }
    if (!std::isfinite(total)) return std::nullopt;
    std::vector<double> cumulative;
    cumulative.reserve(weights.size());
    double running = 0.0;
    for (const double weight : weights) {
        running += weight;
        cumulative.push_back(running / total);
    }
    return VwapSchedule(params, std::move(cumulative));
}

VwapSchedule::VwapSchedule(const SliceParams& params, std::vector<double> cumulative_fraction)
    : Schedule(params), cumulative_fraction_(std::move(cumulative_fraction)) {}

double VwapSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const auto& params = slice_params();
    const std::int64_t released = released_slices(params, now_ns);
    if (released == 0) return 0.0;
    if (released == params.num_slices) return params.total_qty;
    return params.total_qty * cumulative_fraction_[static_cast<std::size_t>(released - 1)];
}

}  // namespace slipstream
