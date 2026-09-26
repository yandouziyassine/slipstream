#pragma once

#include <grpcpp/grpcpp.h>

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include "config.h"
#include "engine_loop.h"
#include "slipstream/v1/execution.pb.h"

namespace slipstream {

// A validated item ready for the engine queue, or the status that rejects the event.
using Admission = std::variant<MarketItem, grpc::Status>;

// The boundary checks shared by the unary calls and the market streams. An admitted item always
// passes the engine's own checks, so the engine never has to reject queued data.
class MarketValidator {
public:
    static constexpr int kMaxLevelsPerUpdate = 1000;
    static constexpr int kMaxTradesPerBatch = 1000;

    MarketValidator(std::string symbol, std::vector<std::string> venues);

    // An empty name means the only venue, and resolves only when exactly one is registered.
    std::optional<std::size_t> resolve_venue(std::string_view name) const;
    const std::string& venue_name(std::size_t venue) const { return venues_.at(venue); }

    // The venue is already resolved; the event's own venue field is not read.
    Admission book(const v1::BookUpdate& update, std::size_t venue) const;
    Admission trades(const v1::TradeBatch& batch, std::size_t venue) const;

private:
    std::string symbol_;
    std::vector<std::string> venues_;
};

// The per-stream rules of one MarketStream on top of MarketValidator.
// Live: every event belongs to the bound venue and carries no client time.
// Replay: book and trade events name their venue, and time never goes backwards or jumps more
// than kMaxReplayJumpNs past the previous timed event.
class StreamAdmission {
public:
    static constexpr std::int64_t kMaxReplayJumpNs = 86'400'000'000'000;

    static StreamAdmission live(const MarketValidator& validator, std::size_t venue);
    static StreamAdmission replay(const MarketValidator& validator);

    Admission admit(const v1::MarketEvent& event);

private:
    StreamAdmission(const MarketValidator& validator, ClockMode mode, std::size_t venue);

    Admission admit_live(const v1::MarketEvent& event) const;
    Admission admit_replay(const v1::MarketEvent& event);
    std::optional<std::size_t> live_venue(std::string_view name) const;
    std::optional<std::size_t> replay_venue(std::string_view name) const;
    // Checks and records the time of a timed replay event.
    bool advance(std::int64_t time_ns);

    const MarketValidator& validator_;
    ClockMode mode_;
    std::size_t venue_;
    std::optional<std::int64_t> last_ns_;
};

}  // namespace slipstream
