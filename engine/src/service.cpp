#include "service.h"

#include <cstdint>
#include <optional>
#include <utility>
#include <vector>

#include "schedule_spec.h"
#include "slicing.h"

namespace slipstream {
namespace {

std::vector<Level> to_levels(const google::protobuf::RepeatedPtrField<v1::PriceLevel>& levels) {
    std::vector<Level> out;
    out.reserve(static_cast<std::size_t>(levels.size()));
    for (const auto& level : levels) out.push_back({level.price(), level.qty()});
    return out;
}

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

}  // namespace

ExecutionService::ExecutionService(Engine& engine, std::string symbol)
    : engine_(engine), symbol_(std::move(symbol)) {}

std::optional<std::size_t> ExecutionService::resolve_venue(const std::string& name) const {
    if (name.empty()) {
        return engine_.venue_count() == 1 ? std::optional<std::size_t>{0} : std::nullopt;
    }
    return engine_.venue_index(name);
}

grpc::Status ExecutionService::ApplyBookUpdate(grpc::ServerContext*, const v1::BookUpdate* request,
                                               v1::BookAck*) {
    if (request->symbol() != symbol_) return invalid("unexpected symbol");
    if (request->bids_size() > kMaxLevelsPerUpdate || request->asks_size() > kMaxLevelsPerUpdate) {
        return invalid("too many levels");
    }
    const auto venue = resolve_venue(request->venue());
    if (!venue) return invalid("unknown venue");
    if (request->recv_ns() < 0) return invalid("invalid recv_ns");
    const auto bids = to_levels(request->bids());
    const auto asks = to_levels(request->asks());
    const bool ok = request->is_snapshot()
                        ? engine_.apply_book_snapshot(*venue, bids, asks, request->recv_ns())
                        : engine_.apply_book_update(*venue, bids, asks, request->recv_ns());
    if (!ok) return invalid("invalid price level");
    return grpc::Status::OK;
}

grpc::Status ExecutionService::SubmitParentOrder(grpc::ServerContext*,
                                                 const v1::ParentOrder* request,
                                                 v1::SubmitReply* reply) {
    const auto side = from_proto(request->side());
    if (!side) return invalid("invalid side");
    const auto spec = schedule_from_proto(*request);
    if (!spec) return invalid("too many vwap weights");
    const auto result = engine_.submit({request->order_id(), *side, request->qty(),
                                        request->start_ns(), request->duration_ns(),
                                        request->num_slices()},
                                       *spec);
    reply->set_accepted(result.accepted);
    reply->set_reason(result.reason);
    return grpc::Status::OK;
}

grpc::Status ExecutionService::Step(grpc::ServerContext*, const v1::StepRequest* request,
                                    v1::StepReply* reply) {
    for (const auto& fill : engine_.step(request->now_ns()).fills) {
        auto* out = reply->add_fills();
        out->set_order_id(fill.order_id);
        out->set_ts_ns(fill.ts_ns);
        out->set_qty(fill.qty);
        out->set_price(fill.price);
        out->set_venue(fill.venue);
        out->set_fee(fill.fee);
    }
    reply->set_working_orders(static_cast<std::int32_t>(engine_.working_orders()));
    return grpc::Status::OK;
}

grpc::Status ExecutionService::GetStatus(grpc::ServerContext*, const v1::StatusRequest*,
                                         v1::StatusReply* reply) {
    reply->set_position(engine_.position());
    reply->set_book_depth(static_cast<std::int32_t>(engine_.book_depth()));
    if (const auto mid = engine_.mid()) {
        reply->set_has_mid(true);
        reply->set_mid(*mid);
    }
    for (const auto& status : engine_.statuses()) {
        auto* out = reply->add_orders();
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
    for (const auto& venue : engine_.venue_settings()) {
        auto* info = reply->add_venues();
        info->set_name(venue.name);
        info->set_fee_bps(venue.fee_bps);
        info->set_min_qty(venue.min_qty);
        info->set_qty_step(venue.qty_step);
        info->set_min_notional(venue.min_notional);
    }
    return grpc::Status::OK;
}

grpc::Status ExecutionService::ApplyTrades(grpc::ServerContext*, const v1::TradeBatch* request,
                                           v1::TradeAck*) {
    if (request->symbol() != symbol_) return invalid("unexpected symbol");
    if (request->trades_size() > kMaxTradesPerBatch) return invalid("too many trades");
    if (!resolve_venue(request->venue())) return invalid("unknown venue");
    std::vector<Trade> trades;
    trades.reserve(static_cast<std::size_t>(request->trades_size()));
    for (const auto& trade : request->trades()) trades.push_back({trade.price(), trade.qty()});
    if (!engine_.apply_trades(trades)) return invalid("invalid trade");
    return grpc::Status::OK;
}

}  // namespace slipstream
