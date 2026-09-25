#include "router.h"

#include <algorithm>
#include <iterator>

namespace slipstream {
namespace {

struct Candidate {
    double effective_price;
    std::size_t venue;
    double fee_rate;
    Level level;
};

}  // namespace

RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues) {
    std::vector<Candidate> candidates;
    for (const auto& venue : venues) {
        const double multiplier =
            taker_side == Side::Buy ? 1.0 + venue.fee_rate : 1.0 - venue.fee_rate;
        for (const auto& level : venue.levels) {
            candidates.push_back({level.price * multiplier, venue.venue, venue.fee_rate, level});
        }
    }
    std::stable_sort(candidates.begin(), candidates.end(),
                     [taker_side](const Candidate& a, const Candidate& b) {
                         return taker_side == Side::Buy ? a.effective_price < b.effective_price
                                                        : a.effective_price > b.effective_price;
                     });

    RouteResult result;
    double remaining = qty;
    for (const auto& candidate : candidates) {
        if (remaining <= 0.0) break;
        const double take = std::min(remaining, candidate.level.qty);
        const double notional = take * candidate.level.price;
        const double fee = notional * candidate.fee_rate;
        auto leg = std::find_if(result.legs.begin(), result.legs.end(),
                                [&](const RouteLeg& l) { return l.venue == candidate.venue; });
        if (leg == result.legs.end()) {
            result.legs.push_back({candidate.venue, 0.0, 0.0, 0.0});
            leg = std::prev(result.legs.end());
        }
        leg->qty += take;
        leg->gross_notional += notional;
        leg->fee += fee;
        result.filled_qty += take;
        result.gross_notional += notional;
        result.fees += fee;
        remaining -= take;
    }
    std::sort(result.legs.begin(), result.legs.end(),
              [](const RouteLeg& a, const RouteLeg& b) { return a.venue < b.venue; });
    return result;
}

double all_in_notional(Side side, const RouteResult& result) {
    return side == Side::Buy ? result.gross_notional + result.fees
                             : result.gross_notional - result.fees;
}

}  // namespace slipstream
