#include "twap.h"

namespace slipstream {

std::optional<TwapSchedule> TwapSchedule::create(const SliceParams& params) {
    if (!valid_slice_params(params)) return std::nullopt;
    return TwapSchedule(params);
}

double TwapSchedule::target_qty_at(std::int64_t now_ns, const MarketState&) const {
    const auto& params = slice_params();
    const std::int64_t released = released_slices(params, now_ns);
    if (released == params.num_slices) return params.total_qty;
    return params.total_qty * static_cast<double>(released) /
           static_cast<double>(params.num_slices);
}

}  // namespace slipstream
