#include "schedule_factory.h"

#include <optional>
#include <utility>

#include "almgren_chriss.h"
#include "pov.h"
#include "twap.h"
#include "vwap.h"

namespace slipstream {
namespace {

template <typename T>
std::unique_ptr<Schedule> own(std::optional<T> schedule) {
    if (!schedule) return nullptr;
    return std::make_unique<T>(std::move(*schedule));
}

struct Builder {
    const SliceParams& params;
    const MarketState& at_submit;

    std::unique_ptr<Schedule> operator()(const TwapSpec&) const {
        return own(TwapSchedule::create(params));
    }
    std::unique_ptr<Schedule> operator()(const VwapSpec& spec) const {
        return own(VwapSchedule::create(params, spec.weights));
    }
    std::unique_ptr<Schedule> operator()(const AlmgrenChrissSpec& spec) const {
        return own(AlmgrenChrissSchedule::create(params, spec.sigma, spec.eta, spec.risk_aversion));
    }
    std::unique_ptr<Schedule> operator()(const PovSpec& spec) const {
        return own(PovSchedule::create(params, spec.participation, at_submit.cumulative_volume));
    }
};

}  // namespace

std::unique_ptr<Schedule> make_schedule(const SliceParams& params, const ScheduleSpec& spec,
                                        const MarketState& at_submit) {
    return std::visit(Builder{params, at_submit}, spec);
}

}  // namespace slipstream
