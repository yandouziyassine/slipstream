#include "service.h"

#include <cstdint>
#include <iterator>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <utility>
#include <vector>

#include "schedule_spec.h"
#include "slicing.h"

namespace slipstream {
namespace {

std::optional<Side> from_proto(v1::Side side) {
    switch (side) {
        case v1::SIDE_BUY:
            return Side::Buy;
        case v1::SIDE_SELL:
            return Side::Sell;
        default:
            return std::nullopt;
    }
}

v1::Side to_proto(Side side) { return side == Side::Buy ? v1::SIDE_BUY : v1::SIDE_SELL; }

v1::OrderState to_proto(OrderState state) {
    switch (state) {
        case OrderState::Working:
            return v1::ORDER_STATE_WORKING;
        case OrderState::Completed:
            return v1::ORDER_STATE_COMPLETED;
        case OrderState::Halted:
            return v1::ORDER_STATE_HALTED;
    }
    return v1::ORDER_STATE_UNSPECIFIED;
}

grpc::Status invalid(const char* message) {
    return grpc::Status(grpc::StatusCode::INVALID_ARGUMENT, message);
}

// run() throws std::runtime_error when the loop stopped or its command queue is full.
template <class F>
grpc::Status on_loop(F&& body) {
    try {
        return body();
    } catch (const std::runtime_error& error) {
        return grpc::Status(grpc::StatusCode::UNAVAILABLE, error.what());
    }
}

std::optional<ScheduleSpec> schedule_from_proto(const v1::ParentOrder& order) {
    switch (order.schedule_case()) {
        case v1::ParentOrder::SCHEDULE_NOT_SET:
        case v1::ParentOrder::kTwap:
            return TwapSpec{};
        case v1::ParentOrder::kVwap: {
            const auto& weights = order.vwap().weights();
            if (weights.size() > kMaxSlices) return std::nullopt;
            return VwapSpec{std::vector<double>(weights.begin(), weights.end())};
        }
        case v1::ParentOrder::kAlmgrenChriss:
            return AlmgrenChrissSpec{order.almgren_chriss().sigma(), order.almgren_chriss().eta(),
                                     order.almgren_chriss().risk_aversion()};
        case v1::ParentOrder::kPov:
            return PovSpec{order.pov().participation()};
    }
    return std::nullopt;
}

void to_proto(const Fill& fill, v1::Fill& out) {
    out.set_order_id(fill.order_id);
    out.set_ts_ns(fill.ts_ns);
    out.set_qty(fill.qty);
    out.set_price(fill.price);
    out.set_venue(fill.venue);
    out.set_fee(fill.fee);
}

void to_proto(const std::vector<Level>& levels,
              google::protobuf::RepeatedPtrField<v1::PriceLevel>& out) {
    for (const auto& level : levels) {
        auto* added = out.Add();
        added->set_price(level.price);
        added->set_qty(level.qty);
    }
}

void add_order(const OrderStatus& status, v1::StatusReply& reply) {
    auto* out = reply.add_orders();
    out->set_order_id(status.order_id);
    out->set_side(to_proto(status.side));
    out->set_state(to_proto(status.state));
    out->set_total_qty(status.total_qty);
    out->set_filled_qty(status.filled_qty);
    out->set_avg_fill_price(status.avg_fill_price);
    out->set_arrival_mid(status.arrival_mid);
    out->set_slippage_bps(status.slippage_bps);
    out->set_immediate_cost_bps(status.immediate_cost_bps);
    out->set_halt_reason(status.halt_reason);
    out->set_algo(status.algo);
    out->set_fees_paid(status.fees_paid);
    out->set_fees_bps(status.fees_bps);
    out->set_routed_all_in_bps(status.routed_all_in_bps);
    out->set_immediate_filled_qty(status.immediate_filled_qty);
    for (const auto& venue_cost : status.venue_costs) {
        auto* out_cost = out->add_venue_costs();
        out_cost->set_venue(venue_cost.venue);
        out_cost->set_all_in_bps(venue_cost.all_in_bps);
        out_cost->set_available(venue_cost.available);
    }
}

// Runs on the engine thread, so every field comes from the same moment.
void fill_status(const Engine& engine, const LoopView& view, ClockMode mode,
                 v1::StatusReply& reply) {
    reply.set_position(engine.position());
    reply.set_book_depth(static_cast<std::int32_t>(engine.book_depth()));
    if (const auto mid = engine.mid()) {
        reply.set_has_mid(true);
        reply.set_mid(*mid);
    }
    for (const auto& status : engine.statuses()) add_order(status, reply);
    const auto settings = engine.venue_settings();
    const auto states = engine.venue_states(view.now_ns);
    for (std::size_t i = 0; i < settings.size(); ++i) {
        auto* info = reply.add_venues();
        info->set_name(settings[i].name);
        info->set_fee_bps(settings[i].fee_bps);
        info->set_min_qty(settings[i].min_qty);
        info->set_qty_step(settings[i].qty_step);
        info->set_min_notional(settings[i].min_notional);
        info->set_has_book(states.at(i).has_book);
        info->set_fresh(states.at(i).fresh);
    }
    for (const auto& book : engine.books(engine.book_depth())) {
        auto* out = reply.add_books();
        out->set_venue(book.venue);
        to_proto(book.bids, *out->mutable_bids());
        to_proto(book.asks, *out->mutable_asks());
    }
    auto* stats = reply.mutable_stats();
    stats->set_events(view.stats.events);
    stats->set_queue_high_water(view.stats.queue_high_water);
    stats->set_latency_p50_ns(view.stats.p50_ns);
    stats->set_latency_p99_ns(view.stats.p99_ns);
    reply.set_clock_mode(mode == ClockMode::Live ? v1::CLOCK_MODE_LIVE : v1::CLOCK_MODE_REPLAY);
}

v1::EngineEvent to_proto(const EngineEventData& data) {
    v1::EngineEvent out;
    out.set_seq(data.seq);
    if (const auto* fill = std::get_if<Fill>(&data.event)) {
        to_proto(*fill, *out.mutable_fill());
    } else {
        const auto& update = std::get<OrderUpdate>(data.event);
        auto* order = out.mutable_order();
        order->set_order_id(update.order_id);
        order->set_state(to_proto(update.state));
        order->set_reason(update.reason);
        order->set_filled_qty(update.filled_qty);
    }
    return out;
}

// Runs its function on every exit path of the enclosing scope.
template <class F>
class ScopeExit {
public:
    explicit ScopeExit(F fn) : fn_(std::move(fn)) {}
    ~ScopeExit() { fn_(); }
    ScopeExit(const ScopeExit&) = delete;
    ScopeExit& operator=(const ScopeExit&) = delete;

private:
    F fn_;
};

std::vector<std::string> venue_names(EngineLoop& loop) {
    std::vector<std::string> names;
    for (const auto& venue : loop.run([](Engine& e) { return e.venue_settings(); })) {
        names.push_back(venue.name);
    }
    return names;
}

}  // namespace

ExecutionService::ExecutionService(EngineLoop& loop, std::string symbol)
    : loop_(loop),
      validator_(std::move(symbol), venue_names(loop),
                 loop.run([](Engine& engine) { return engine.book_depth(); })) {}

std::int64_t ExecutionService::now_or(std::int64_t client_ns) const {
    return loop_.mode() == ClockMode::Live ? loop_.live_now_ns() : client_ns;
}

grpc::Status ExecutionService::ApplyBookUpdate(grpc::ServerContext*, const v1::BookUpdate* request,
                                               v1::BookAck*) {
    const auto venue = validator_.resolve_venue(request->venue());
    if (!venue) return invalid("unknown venue");
    auto admitted = validator_.book(*request, *venue);
    if (const auto* status = std::get_if<grpc::Status>(&admitted)) return *status;
    const auto& book = std::get<BookData>(std::get<MarketItem>(admitted).data);
    return on_loop([&] {
        const bool ok = loop_.run([&](Engine& engine) {
            const auto recv_ns = now_or(book.recv_ns);
            return book.is_snapshot
                       ? engine.apply_book_snapshot(*venue, book.bids, book.asks, recv_ns)
                       : engine.apply_book_update(*venue, book.bids, book.asks, recv_ns);
        });
        return ok ? grpc::Status::OK : invalid("invalid price level");
    });
}

grpc::Status ExecutionService::SubmitParentOrder(grpc::ServerContext*,
                                                 const v1::ParentOrder* request,
                                                 v1::SubmitReply* reply) {
    const auto side = from_proto(request->side());
    if (!side) return invalid("invalid side");
    auto spec = schedule_from_proto(*request);
    if (!spec) return invalid("too many vwap weights");
    ParentOrderRequest order{request->order_id(),    *side,
                             request->qty(),         request->start_ns(),
                             request->duration_ns(), request->num_slices()};
    return on_loop([&] {
        const auto result = loop_.submit_and_step(std::move(order), std::move(*spec));
        reply->set_accepted(result.accepted);
        reply->set_reason(result.reason);
        return grpc::Status::OK;
    });
}

grpc::Status ExecutionService::Step(grpc::ServerContext*, const v1::StepRequest* request,
                                    v1::StepReply* reply) {
    return on_loop([&] {
        loop_.run([&](Engine& engine) {
            for (const auto& fill : engine.step(now_or(request->now_ns())).fills) {
                to_proto(fill, *reply->add_fills());
            }
            reply->set_working_orders(static_cast<std::int32_t>(engine.working_orders()));
        });
        return grpc::Status::OK;
    });
}

grpc::Status ExecutionService::GetStatus(grpc::ServerContext*, const v1::StatusRequest*,
                                         v1::StatusReply* reply) {
    return on_loop([&] {
        loop_.inspect([&](Engine& engine, const LoopView& view) {
            fill_status(engine, view, loop_.mode(), *reply);
        });
        return grpc::Status::OK;
    });
}

grpc::Status ExecutionService::ApplyTrades(grpc::ServerContext*, const v1::TradeBatch* request,
                                           v1::TradeAck*) {
    const auto venue = validator_.resolve_venue(request->venue());
    if (!venue) return invalid("unknown venue");
    auto admitted = validator_.trades(*request, *venue);
    if (const auto* status = std::get_if<grpc::Status>(&admitted)) return *status;
    const auto& trades = std::get<TradeData>(std::get<MarketItem>(admitted).data).trades;
    return on_loop([&] {
        const bool ok = loop_.run([&](Engine& engine) { return engine.apply_trades(trades); });
        return ok ? grpc::Status::OK : invalid("invalid trade");
    });
}

std::optional<std::size_t> ExecutionService::metadata_venue(
    const grpc::ServerContext& context) const {
    const auto& metadata = context.client_metadata();
    const auto [first, last] = metadata.equal_range(kVenueMetadataKey);
    if (first == last || std::next(first) != last) return std::nullopt;
    const std::string_view name(first->second.data(), first->second.size());
    if (name.empty()) return std::nullopt;
    return validator_.resolve_venue(name);
}

grpc::Status ExecutionService::MarketStream(grpc::ServerContext* context,
                                            grpc::ServerReader<v1::MarketEvent>* reader,
                                            v1::MarketStreamSummary* summary) {
    if (loop_.mode() == ClockMode::Replay) {
        if (!loop_.bind_replay()) {
            return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION,
                                "a replay stream is already open");
        }
        const ScopeExit unbind([this] { loop_.unbind_replay(); });
        return pump(StreamAdmission::replay(validator_), *reader, *summary);
    }
    const auto venue = metadata_venue(*context);
    if (!venue) return invalid("slipstream-venue metadata must name one registered venue");
    if (!loop_.bind_venue(*venue)) {
        return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION,
                            "this venue already has an open stream");
    }
    const ScopeExit unbind([this, bound = *venue] { loop_.unbind_venue(bound); });
    return pump(StreamAdmission::live(validator_, *venue), *reader, *summary);
}

grpc::Status ExecutionService::pump(StreamAdmission admission,
                                    grpc::ServerReader<v1::MarketEvent>& reader,
                                    v1::MarketStreamSummary& summary) {
    v1::MarketEvent event;
    std::uint64_t events = 0;
    while (reader.Read(&event)) {
        auto admitted = admission.admit(event);
        if (const auto* status = std::get_if<grpc::Status>(&admitted)) return *status;
        if (!loop_.push(std::move(std::get<MarketItem>(admitted)))) {
            return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED, "engine queue full");
        }
        ++events;
    }
    summary.set_events(events);
    return grpc::Status::OK;
}

grpc::Status ExecutionService::Subscribe(grpc::ServerContext* context,
                                         const v1::SubscribeRequest*,
                                         grpc::ServerWriter<v1::EngineEvent>* writer) {
    const auto subscriber = loop_.subscribe();
    if (!subscriber) {
        return grpc::Status(grpc::StatusCode::FAILED_PRECONDITION, "another subscriber is active");
    }
    const ScopeExit unsubscribe([&] { loop_.unsubscribe(subscriber); });
    writer->SendInitialMetadata();
    for (;;) {
        if (context->IsCancelled()) return grpc::Status::OK;
        if (subscriber->overflowed()) {
            return grpc::Status(grpc::StatusCode::RESOURCE_EXHAUSTED,
                                "subscriber fell too far behind; read order state from GetStatus");
        }
        const auto event = subscriber->pop_for(kSubscribePoll);
        if (event) {
            if (!writer->Write(to_proto(*event))) return grpc::Status::OK;  // the client left
            continue;
        }
        if (subscriber->closed()) return grpc::Status::OK;
    }
}

}  // namespace slipstream
