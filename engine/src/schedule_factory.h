#pragma once

#include <memory>

#include "schedule.h"
#include "schedule_spec.h"
#include "slicing.h"

namespace slipstream {

// nullptr when the parameters are invalid for the requested schedule.
std::unique_ptr<Schedule> make_schedule(const SliceParams& params, const ScheduleSpec& spec,
                                        const MarketState& at_submit);

}  // namespace slipstream
