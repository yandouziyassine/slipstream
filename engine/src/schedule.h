#pragma once

#include <cstdint>

#include "slicing.h"

namespace slipstream {

struct MarketState {
    double cumulative_volume;
};

class Schedule {
public:
    virtual ~Schedule() = default;

    // Cumulative quantity that should be complete by now_ns.
    virtual double target_qty_at(std::int64_t now_ns, const MarketState& market) const = 0;

    // True from start + duration on: the unfilled remainder is then halted, never worked further.
    bool expired(std::int64_t now_ns) const {
        return now_ns >= params_.start_ns && now_ns - params_.start_ns >= params_.duration_ns;
    }

    virtual const char* name() const = 0;

protected:
    explicit Schedule(const SliceParams& params) : params_(params) {}
    Schedule(const Schedule&) = default;
    Schedule& operator=(const Schedule&) = default;

    const SliceParams& slice_params() const { return params_; }

private:
    SliceParams params_;
};

}  // namespace slipstream
