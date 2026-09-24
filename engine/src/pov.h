#pragma once

#include <optional>

#include "schedule.h"
#include "slicing.h"

namespace slipstream {

class PovSchedule final : public Schedule {
public:
    static constexpr double kMaxParticipation = 0.5;

    static std::optional<PovSchedule> create(const SliceParams& params, double participation,
                                             double volume_at_submit);

    double target_qty_at(std::int64_t now_ns, const MarketState& market) const override;
    bool expired(std::int64_t now_ns) const override;
    const char* name() const override { return "pov"; }

private:
    PovSchedule(const SliceParams& params, double participation, double volume_at_submit);

    SliceParams params_;
    double participation_;
    double volume_at_submit_;
};

}  // namespace slipstream
