#pragma once

#include <cstdint>

namespace slipstream {

struct MarketState {
    double cumulative_volume;
};

class Schedule {
public:
    virtual ~Schedule() = default;

    // Cumulative quantity that should be complete by now_ns.
    virtual double target_qty_at(std::int64_t now_ns, const MarketState& market) const = 0;

    // True once the unfilled remainder should be halted instead of worked further.
    virtual bool expired(std::int64_t /*now_ns*/) const { return false; }

    virtual const char* name() const = 0;

protected:
    Schedule() = default;
    Schedule(const Schedule&) = default;
    Schedule& operator=(const Schedule&) = default;
};

}  // namespace slipstream
