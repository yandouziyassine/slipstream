#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "order_book.h"
#include "types.h"

namespace slipstream {

// What one order has taken from one side of one venue's book. It is keyed by level version, so
// when the feed sets a level again, the record for the old version stops matching and the order
// sees the fresh quantity. The book itself is never changed.
class ConsumptionOverlay {
public:
    // The displayed levels minus what this order took, best-first. Emptied levels are left out.
    std::vector<Level> remaining(const std::vector<BookLevel>& levels) const;
    // taken[i] was taken from the i-th level of remaining(levels); taken may be shorter.
    // Entries for levels that are no longer in `levels` are dropped.
    void record(const std::vector<BookLevel>& levels, const std::vector<double>& taken);
    void clear();
    std::size_t size() const;

private:
    struct Entry {
        std::uint64_t version;
        double taken;
    };

    double taken_at(std::uint64_t version) const;

    std::vector<Entry> entries_;  // sorted by version
};

}  // namespace slipstream
