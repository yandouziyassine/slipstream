#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class TwapSchedule final : public Schedule {
public:
    static std::optional<TwapSchedule> create(const SliceParams& params);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    const char* name() const override { return "twap"; }

private:
    explicit TwapSchedule(const SliceParams& params) : params_(params) {}

    SliceParams params_;
};

}  // namespace slipstream
