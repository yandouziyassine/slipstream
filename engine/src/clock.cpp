#include "clock.h"

#include <chrono>

namespace slipstream {
namespace {

std::int64_t to_ns(std::chrono::system_clock::time_point tp) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(tp.time_since_epoch()).count();
}

std::int64_t to_ns(std::chrono::steady_clock::time_point tp) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(tp.time_since_epoch()).count();
}

}  // namespace

LiveClock::LiveClock()
    : anchor_system_ns_(to_ns(std::chrono::system_clock::now())),
      anchor_steady_ns_(to_ns(std::chrono::steady_clock::now())) {}

std::int64_t LiveClock::now_ns() const {
    return anchor_system_ns_ + (to_ns(std::chrono::steady_clock::now()) - anchor_steady_ns_);
}

}  // namespace slipstream
