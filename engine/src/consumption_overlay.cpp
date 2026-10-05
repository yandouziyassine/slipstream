#include "consumption_overlay.h"

#include <algorithm>
#include <utility>

namespace slipstream {
namespace {

// Float noise only: takes that add up to a level up to rounding must use it all up.
constexpr double kDustFraction = 1e-9;

bool has_left(const BookLevel& level, double taken) {
    return level.qty - taken > level.qty * kDustFraction;
}

}  // namespace

double ConsumptionOverlay::taken_at(std::uint64_t version) const {
    const auto it = std::lower_bound(
        entries_.begin(), entries_.end(), version,
        [](const Entry& entry, std::uint64_t wanted) { return entry.version < wanted; });
    return it != entries_.end() && it->version == version ? it->taken : 0.0;
}

std::vector<Level> ConsumptionOverlay::remaining(const std::vector<BookLevel>& levels) const {
    std::vector<Level> out;
    out.reserve(levels.size());
    for (const auto& level : levels) {
        const double taken = taken_at(level.version);
        if (has_left(level, taken)) out.push_back({level.price, level.qty - taken});
    }
    return out;
}

void ConsumptionOverlay::record(const std::vector<BookLevel>& levels,
                                const std::vector<double>& taken) {
    std::vector<Entry> next;
    next.reserve(levels.size());
    std::size_t visible = 0;
    for (const auto& level : levels) {
        double total = taken_at(level.version);
        // Same test as remaining(), so taken[i] lines up with the i-th level it returned.
        if (has_left(level, total)) {
            if (visible < taken.size()) total += taken[visible];
            ++visible;
        }
        if (total > 0.0) next.push_back({level.version, total});
    }
    std::sort(next.begin(), next.end(),
              [](const Entry& a, const Entry& b) { return a.version < b.version; });
    entries_ = std::move(next);
}

void ConsumptionOverlay::clear() { std::vector<Entry>().swap(entries_); }

std::size_t ConsumptionOverlay::size() const { return entries_.size(); }

}  // namespace slipstream
