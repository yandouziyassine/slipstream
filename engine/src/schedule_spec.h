#pragma once

#include <variant>
#include <vector>

namespace slipstream {

struct TwapSpec {};

struct VwapSpec {
    std::vector<double> weights;
};

struct AlmgrenChrissSpec {
    double sigma;
    double eta;
    double risk_aversion;
};

struct PovSpec {
    double participation;
};

using ScheduleSpec = std::variant<TwapSpec, VwapSpec, AlmgrenChrissSpec, PovSpec>;

}  // namespace slipstream
