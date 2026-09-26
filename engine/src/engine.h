#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "order_book.h"
#include "risk.h"
#include "router.h"
#include "schedule.h"
#include "schedule_spec.h"
#include "types.h"

namespace slipstream {

enum class OrderState { Working, Completed, Halted };

struct ParentOrderRequest {
    std::string order_id;
    Side side;
    double qty;
    std::int64_t start_ns;
    std::int64_t duration_ns;
    std::int32_t num_slices;
};

struct SubmitResult {
    bool accepted;
    std::string reason;
};

struct Fill {
    std::string order_id;
    std::int64_t ts_ns;
    double qty;
    double price;
    std::string venue;
    double fee;
};

struct OrderUpdate {
    std::string order_id;
    OrderState state;
    std::string reason;
    double filled_qty;
};

// Updates list each order whose state or filled quantity changed during the step.
struct StepOutput {
    std::vector<Fill> fills;
    std::vector<OrderUpdate> updates;
};

struct VenueSettings {
    std::string name;
    double fee_bps;
    double min_qty = 0.0;
    double qty_step = 0.0;
    double min_notional = 0.0;
};

struct VenueCost {
    std::string venue;
    double all_in_bps;
    bool available;
};

struct OrderStatus {
    std::string order_id;
    Side side;
    OrderState state;
    double total_qty;
    double filled_qty;
    double avg_fill_price;
    double arrival_mid;
    double slippage_bps;
    double immediate_cost_bps;
    std::string halt_reason;
    std::string algo;
    double fees_paid;
    double fees_bps;
    double routed_all_in_bps;
    std::vector<VenueCost> venue_costs;
    double immediate_filled_qty;
};

class Engine {
public:
    static constexpr std::size_t kMaxOrders = 1000;
    static constexpr std::size_t kMaxOrderIdLength = 64;

    Engine(RiskLimits limits, std::size_t book_depth);
    // Widest collar the --max-deviation-bps flag allows; the flag itself defaults to 50.
    static constexpr double kWidestDeviationBps = 10000.0;

    Engine(RiskLimits limits, std::size_t book_depth, std::vector<VenueSettings> venues,
           std::int64_t stale_ns, double max_deviation_bps = kWidestDeviationBps);

    bool apply_book_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks);
    bool apply_book_update(const std::vector<Level>& bids, const std::vector<Level>& asks);
    bool apply_book_snapshot(std::size_t venue, const std::vector<Level>& bids,
                             const std::vector<Level>& asks, std::int64_t recv_ns);
    bool apply_book_update(std::size_t venue, const std::vector<Level>& bids,
                           const std::vector<Level>& asks, std::int64_t recv_ns);
    bool apply_trades(const std::vector<Trade>& trades);
    // A heartbeat proves the feed is alive, so it keeps a quiet venue fresh, but only for
    // kHeartbeatGraceNs after the venue's last real book change.
    bool apply_heartbeat(std::size_t venue, std::int64_t now_ns);
    static constexpr std::int64_t kHeartbeatGraceNs = 30'000'000'000;

    SubmitResult submit(const ParentOrderRequest& request, const ScheduleSpec& spec = TwapSpec{});
    StepOutput step(std::int64_t now_ns);

    std::vector<OrderStatus> statuses() const;
    std::size_t working_orders() const;
    std::size_t book_depth() const;
    double position() const;
    std::optional<double> mid() const;
    double market_volume() const;
    std::optional<std::size_t> venue_index(std::string_view name) const;
    std::size_t venue_count() const;
    std::vector<VenueSettings> venue_settings() const;

private:
    struct ParentOrder {
        ParentOrderRequest request;
        std::unique_ptr<Schedule> schedule;
        OrderState state;
        double filled_qty;
        double filled_notional;
        double fees;
        double arrival_mid;
        double immediate_cost_bps;
        double immediate_filled_qty;
        std::string halt_reason;
        std::vector<double> venue_all_in_notional;
        std::vector<char> venue_available;
    };

    struct Venue {
        VenueSettings settings;
        double fee_rate;
        OrderBook book;
        std::int64_t last_update_ns;
        std::int64_t last_heartbeat_ns;
    };

    // The remainder can never trade when it is below every venue's minimum.
    static bool complete_if_below_minimum(ParentOrder& order, double minimum);
    double registered_minimum_locked(double ref_price) const;
    double projected_position_locked() const;
    void advance_locked(ParentOrder& order, std::int64_t now_ns, std::optional<double> ref_price,
                        const MarketState& market, std::vector<Fill>& fills);
    bool fresh_locked(std::size_t venue, std::int64_t now_ns) const;
    std::optional<double> consolidated_mid_locked(std::int64_t now_ns) const;
    // Levels beyond the price collar around ref_price are left out.
    std::vector<VenueLiquidity> liquidity_locked(Side side, std::int64_t now_ns,
                                                 std::optional<std::size_t> only,
                                                 double ref_price) const;

    mutable std::mutex mu_;
    std::vector<Venue> venues_;
    std::size_t book_depth_;
    std::int64_t stale_ns_;
    double max_deviation_;
    std::int64_t latest_ns_ = 0;
    RiskCheck risk_;
    double position_ = 0.0;
    double market_volume_ = 0.0;
    std::vector<ParentOrder> orders_;
};

}  // namespace slipstream
