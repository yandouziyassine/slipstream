#pragma once

#include <optional>
#include <vector>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class VwapSchedule final : public Schedule {
public:
    static std::optional<VwapSchedule> create(const SliceParams& params,
                                              const std::vector<double>& weights);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "vwap"; }

private:
    VwapSchedule(const SliceParams& params, std::vector<double> cumulative_fraction);

    std::vector<double> cumulative_fraction_;
};

}  // namespace slipstream
