#include "twap.h"

#include <algorithm>
#include <cmath>

namespace slipstream {

std::optional<TwapSchedule> TwapSchedule::create(const TwapParams& params) {
    if (!std::isfinite(params.total_qty) || params.total_qty <= 0.0) return std::nullopt;
    if (params.start_ns < 0 || params.duration_ns <= 0) return std::nullopt;
    if (params.num_slices < 1 || params.num_slices > kMaxSlices) return std::nullopt;
    if (params.duration_ns < params.num_slices) return std::nullopt;
    return TwapSchedule(params);
}

TwapSchedule::TwapSchedule(const TwapParams& params)
    : params_(params), interval_ns_(params.duration_ns / params.num_slices) {}

double TwapSchedule::target_qty_at(std::int64_t now_ns) const {
    if (now_ns < params_.start_ns) return 0.0;
    const std::int64_t elapsed = now_ns - params_.start_ns;
    const std::int64_t released =
        std::min<std::int64_t>(params_.num_slices, elapsed / interval_ns_ + 1);
    if (released == params_.num_slices) return params_.total_qty;
    return params_.total_qty * static_cast<double>(released) /
           static_cast<double>(params_.num_slices);
}

}  // namespace slipstream
