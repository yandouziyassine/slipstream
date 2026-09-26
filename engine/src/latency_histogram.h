#pragma once

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>

namespace slipstream {

// Fixed log2 buckets in nanoseconds: bucket i holds [2^i, 2^(i+1)), so the 32 buckets reach
// about 4.3 s. Values outside the range are clamped into the first or last bucket.
class LatencyHistogram {
public:
    static constexpr std::size_t kBuckets = 32;

    void record(std::int64_t ns) {
        const auto value = static_cast<std::uint64_t>(std::max<std::int64_t>(ns, 1));
        const auto bucket = static_cast<std::size_t>(std::bit_width(value)) - 1;
        ++counts_[std::min(bucket, kBuckets - 1)];
        ++total_;
    }

    // The upper bound of the bucket holding the pct-th percentile, or 0 when empty.
    std::int64_t percentile(std::uint64_t pct) const {
        if (total_ == 0) return 0;
        const std::uint64_t rank = (total_ * pct + 99) / 100;
        std::uint64_t seen = 0;
        for (std::size_t i = 0; i < kBuckets; ++i) {
            seen += counts_[i];
            if (seen >= rank) return std::int64_t{1} << (i + 1);
        }
        return std::int64_t{1} << kBuckets;
    }

private:
    std::array<std::uint64_t, kBuckets> counts_{};
    std::uint64_t total_ = 0;
};

}  // namespace slipstream
