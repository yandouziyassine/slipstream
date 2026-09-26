#include "market_validation.h"

#include <algorithm>
#include <cmath>
#include <utility>

#include "order_book.h"

namespace slipstream {
namespace {

grpc::Status invalid(const char* message) {
    return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT, message);
}

std::vector<Level> to_levels(const google::protobuf::RepeatedPtrField<v1::PriceLevel>& levels) {
    std::vector<Level> out;
    out.reserve(static_cast<std::size_t>(levels.size()));
    for (const auto& level : levels) out.push_back({level.price(), level.qty()});
    return out;
}

bool valid_trade(const v1::Trade& trade) {
    return std::isfinite(trade.price()) && std::isfinite(trade.qty()) && trade.price() > 0.0 &&
           trade.qty() > 0.0 && trade.price() <= MarketValidator::kMaxValue &&
           trade.qty() <= MarketValidator::kMaxValue;
}

bool within_caps(const std::vector<Level>& levels) {
    return std::all_of(levels.begin(), levels.end(), [](const Level& level) {
        return level.price <= MarketValidator::kMaxValue && level.qty <= MarketValidator::kMaxValue;
    });
}

}  // namespace

MarketValidator::MarketValidator(std::string symbol, std::vector<std::string> venues,
                                 std::size_t book_depth)
    : symbol_(std::move(symbol)),
      venues_(std::move(venues)),
      stream_max_levels_(static_cast<int>(
          std::clamp<std::size_t>(book_depth, kMinStreamLevels, kMaxLevelsPerUpdate))) {}

std::optional<std::size_t> MarketValidator::resolve_venue(std::string_view name) const {
    if (name.empty()) {
        return venues_.size() == 1 ? std::optional<std::size_t>{0} : std::nullopt;
    }
    const auto found = std::find(venues_.begin(), venues_.end(), name);
    if (found == venues_.end()) return std::nullopt;
    return static_cast<std::size_t>(found - venues_.begin());
}

Admission MarketValidator::book(const v1::BookUpdate& update, std::size_t venue) const {
    if (update.symbol() != symbol_) return invalid("unexpected symbol");
    if (update.bids_size() > kMaxLevelsPerUpdate || update.asks_size() > kMaxLevelsPerUpdate) {
        return invalid("too many levels");
    }
    if (update.recv_ns() < 0) return invalid("invalid recv_ns");
    auto bids = to_levels(update.bids());
    auto asks = to_levels(update.asks());
    const bool allow_zero_qty = !update.is_snapshot();
    if (!OrderBook::valid_levels(bids, allow_zero_qty) ||
        !OrderBook::valid_levels(asks, allow_zero_qty) || !within_caps(bids) ||
        !within_caps(asks)) {
        return invalid("invalid price level");
    }
    return MarketItem{
        venue, BookData{update.is_snapshot(), std::move(bids), std::move(asks), update.recv_ns()},
        0};
}

Admission MarketValidator::trades(const v1::TradeBatch& batch, std::size_t venue) const {
    if (batch.symbol() != symbol_) return invalid("unexpected symbol");
    if (batch.trades_size() > kMaxTradesPerBatch) return invalid("too many trades");
    std::vector<Trade> trades;
    trades.reserve(static_cast<std::size_t>(batch.trades_size()));
    for (const auto& trade : batch.trades()) {
        if (!valid_trade(trade)) return invalid("invalid trade");
        trades.push_back({trade.price(), trade.qty()});
    }
    return MarketItem{venue, TradeData{std::move(trades)}, 0};
}

StreamAdmission StreamAdmission::live(const MarketValidator& validator, std::size_t venue) {
    return StreamAdmission(validator, ClockMode::Live, venue);
}

StreamAdmission StreamAdmission::replay(const MarketValidator& validator) {
    return StreamAdmission(validator, ClockMode::Replay, 0);
}

StreamAdmission::StreamAdmission(const MarketValidator& validator, ClockMode mode,
                                 std::size_t venue)
    : validator_(validator), mode_(mode), venue_(venue) {}

Admission StreamAdmission::admit(const v1::MarketEvent& event) {
    return mode_ == ClockMode::Live ? admit_live(event) : admit_replay(event);
}

Admission StreamAdmission::admit_live(const v1::MarketEvent& event) const {
    switch (event.event_case()) {
        case v1::MarketEvent::kBook: {
            const auto venue = live_venue(event.book().venue());
            if (!venue) return invalid("event venue differs from the stream venue");
            if (event.book().recv_ns() != 0) return invalid("live mode does not accept recv_ns");
            return book(event.book(), *venue);
        }
        case v1::MarketEvent::kTrades: {
            const auto venue = live_venue(event.trades().venue());
            if (!venue) return invalid("event venue differs from the stream venue");
            return validator_.trades(event.trades(), *venue);
        }
        case v1::MarketEvent::kHeartbeat:
            return MarketItem{venue_, HeartbeatData{}, 0};
        case v1::MarketEvent::kTick:
            return invalid("live mode does not accept ticks");
        case v1::MarketEvent::EVENT_NOT_SET:
            break;
    }
    return invalid("empty market event");
}

Admission StreamAdmission::admit_replay(const v1::MarketEvent& event) {
    switch (event.event_case()) {
        case v1::MarketEvent::kBook: {
            const auto venue = replay_venue(event.book().venue());
            if (!venue) return invalid("replay events must name a registered venue");
            if (event.book().recv_ns() <= 0) return invalid("replay books need recv_ns");
            auto admitted = book(event.book(), *venue);
            if (std::holds_alternative<MarketItem>(admitted) && !advance(event.book().recv_ns())) {
                return invalid("replay time went backwards or jumped more than a day");
            }
            return admitted;
        }
        case v1::MarketEvent::kTrades: {
            const auto venue = replay_venue(event.trades().venue());
            if (!venue) return invalid("replay events must name a registered venue");
            return validator_.trades(event.trades(), *venue);
        }
        case v1::MarketEvent::kHeartbeat: {
            const auto venue = validator_.resolve_venue("");
            if (!venue) return invalid("a replay heartbeat needs a single-venue engine");
            return MarketItem{*venue, HeartbeatData{}, 0};
        }
        case v1::MarketEvent::kTick: {
            const auto now_ns = event.tick().now_ns();
            if (now_ns <= 0) return invalid("replay ticks need now_ns");
            if (!advance(now_ns)) {
                return invalid("replay time went backwards or jumped more than a day");
            }
            return MarketItem{0, TickData{now_ns}, 0};
        }
        case v1::MarketEvent::EVENT_NOT_SET:
            break;
    }
    return invalid("empty market event");
}

Admission StreamAdmission::book(const v1::BookUpdate& update, std::size_t venue) const {
    const int max_levels = validator_.stream_max_levels();
    if (update.bids_size() > max_levels || update.asks_size() > max_levels) {
        return invalid("too many levels");
    }
    return validator_.book(update, venue);
}

std::optional<std::size_t> StreamAdmission::live_venue(std::string_view name) const {
    if (name.empty() || name == validator_.venue_name(venue_)) return venue_;
    return std::nullopt;
}

std::optional<std::size_t> StreamAdmission::replay_venue(std::string_view name) const {
    if (name.empty()) return std::nullopt;
    return validator_.resolve_venue(name);
}

bool StreamAdmission::advance(std::int64_t time_ns) {
    if (last_ns_ && (time_ns < *last_ns_ || time_ns - *last_ns_ > kMaxReplayJumpNs)) return false;
    last_ns_ = time_ns;
    return true;
}

}  // namespace slipstream
