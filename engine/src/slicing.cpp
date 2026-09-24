#include "slicing.h"

#include <cmath>

namespace slipstream {

bool valid_slice_params(const SliceParams& params) {
    if (!std::isfinite(params.total_qty) || params.total_qty <= 0.0) return false;
    if (params.start_ns < 0 || params.duration_ns <= 0) return false;
    if (params.num_slices < 1 || params.num_slices > kMaxSlices) return false;
    return params.duration_ns >= params.num_slices;
}

std::int64_t released_slices(const SliceParams& params, std::int64_t now_ns) {
    if (now_ns < params.start_ns) return 0;
    const std::int64_t interval_ns = params.duration_ns / params.num_slices;
    const std::int64_t intervals_elapsed = (now_ns - params.start_ns) / interval_ns;
    if (intervals_elapsed >= params.num_slices - 1) return params.num_slices;
    return intervals_elapsed + 1;
}

}  // namespace slipstream
