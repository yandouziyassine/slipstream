#pragma once

#include <cstdint>

namespace slipstream {

// Anchors system_clock once at construction, then advances with steady_clock, so successive
// now_ns() calls never go backwards even if the wall clock is adjusted after startup.
class LiveClock {
public:
    LiveClock();

    std::int64_t now_ns() const;

private:
    std::int64_t anchor_system_ns_;
    std::int64_t anchor_steady_ns_;
};

}  // namespace slipstream
