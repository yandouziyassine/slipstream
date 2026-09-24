#pragma once

#include <cstdint>
#include <optional>

namespace slipstream {

struct TwapParams {
    double total_qty;
    std::int64_t start_ns;
    std::int64_t duration_ns;
    std::int32_t num_slices;
};

class TwapSchedule {
public:
    static constexpr std::int32_t kMaxSlices = 1000;

    static std::optional<TwapSchedule> create(const TwapParams& params);

    // Cumulative quantity that should have been released by now_ns.
    double target_qty_at(std::int64_t now_ns) const;

    const TwapParams& params() const { return params_; }

private:
    explicit TwapSchedule(const TwapParams& params);

    TwapParams params_;
    std::int64_t interval_ns_;
};

}  // namespace slipstream
