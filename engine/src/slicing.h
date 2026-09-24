#pragma once

#include <cstdint>

namespace slipstream {

struct SliceParams {
    double total_qty;
    std::int64_t start_ns;
    std::int64_t duration_ns;
    std::int32_t num_slices;
};

constexpr std::int32_t kMaxSlices = 1000;

bool valid_slice_params(const SliceParams& params);

// 0 before start, 1 at start, +1 per interval, capped at num_slices. Overflow-safe for any now_ns.
std::int64_t released_slices(const SliceParams& params, std::int64_t now_ns);

}  // namespace slipstream
