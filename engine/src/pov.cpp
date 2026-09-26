#include "pov.h"

#include <algorithm>
#include <cmath>

namespace slipstream {

std::optional<PovSchedule> PovSchedule::create(const SliceParams& params, double participation,
                                               double volume_at_submit) {
    if (!valid_slice_params(params)) return std::nullopt;
    if (!std::isfinite(participation) || participation <= 0.0 ||
        participation > kMaxParticipation) {
        return std::nullopt;
    }
    if (!std::isfinite(volume_at_submit) || volume_at_submit < 0.0) return std::nullopt;
    return PovSchedule(params, participation, volume_at_submit);
}

PovSchedule::PovSchedule(const SliceParams& params, double participation, double volume_at_submit)
    : Schedule(params), participation_(participation), volume_at_submit_(volume_at_submit) {}

double PovSchedule::target_qty_at(std::int64_t now_ns, const MarketState& market) const {
    const auto& params = slice_params();
    if (now_ns < params.start_ns) return 0.0;
    const double traded = market.cumulative_volume - volume_at_submit_;
    if (!(traded > 0.0)) return 0.0;
    return std::min(params.total_qty, participation_ * traded);
}

}  // namespace slipstream
