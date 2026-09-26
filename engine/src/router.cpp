#include "router.h"

#include <algorithm>
#include <cmath>

namespace slipstream {
namespace {

// Absorbs division noise so an exact multiple of the step is not floored one step down.
constexpr double kStepEpsilon = 1e-9;
// Relative slack so a leg that equals a venue minimum up to rounding is not dropped.
constexpr double kRuleTolerance = 1e-9;

struct Candidate {
    double effective_price;
    std::size_t index;
    Level level;
};

struct Piece {
    double price;
    double qty;
};

// Pieces are in walk order, so the excess comes off the worst-priced end.
void floor_to_step(std::vector<Piece>& pieces, double step) {
    if (!(step > 0.0)) return;
    double qty = 0.0;
    for (const auto& piece : pieces) qty += piece.qty;
    double excess = qty - std::floor(qty / step + kStepEpsilon) * step;
    while (excess > 0.0 && !pieces.empty()) {
        auto& last = pieces.back();
        if (last.qty > excess) {
            last.qty -= excess;
            break;
        }
        excess -= last.qty;
        pieces.pop_back();
    }
}

bool below(double value, double minimum) { return value < minimum * (1.0 - kRuleTolerance); }

}  // namespace

RouteResult route(Side taker_side, double qty, const std::vector<VenueLiquidity>& venues) {
    if (!(qty > 0.0)) return {};
    std::vector<Candidate> candidates;
    for (std::size_t i = 0; i < venues.size(); ++i) {
        const auto& venue = venues[i];
        const double multiplier =
            taker_side == Side::Buy ? 1.0 + venue.fee_rate : 1.0 - venue.fee_rate;
        for (const auto& level : venue.levels) {
            candidates.push_back({level.price * multiplier, i, level});
        }
    }
    std::stable_sort(candidates.begin(), candidates.end(),
                     [taker_side](const Candidate& a, const Candidate& b) {
                         return taker_side == Side::Buy ? a.effective_price < b.effective_price
                                                        : a.effective_price > b.effective_price;
                     });

    std::vector<std::vector<Piece>> pieces(venues.size());
    double remaining = qty;
    for (const auto& candidate : candidates) {
        if (remaining <= 0.0) break;
        const double take = std::min(remaining, candidate.level.qty);
        pieces[candidate.index].push_back({candidate.level.price, take});
        remaining -= take;
    }

    RouteResult result;
    for (std::size_t i = 0; i < venues.size(); ++i) {
        const auto& venue = venues[i];
        floor_to_step(pieces[i], venue.qty_step);
        RouteLeg leg{venue.venue, 0.0, 0.0, 0.0};
        for (const auto& piece : pieces[i]) {
            const double notional = piece.qty * piece.price;
            leg.qty += piece.qty;
            leg.gross_notional += notional;
            leg.fee += notional * venue.fee_rate;
        }
        if (!(leg.qty > 0.0) || below(leg.qty, venue.min_qty) ||
            below(leg.gross_notional, venue.min_notional)) {
            continue;
        }
        result.legs.push_back(leg);
        result.filled_qty += leg.qty;
        result.gross_notional += leg.gross_notional;
        result.fees += leg.fee;
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
