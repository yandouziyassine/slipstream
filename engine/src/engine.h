#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "order_book.h"
#include "risk.h"
#include "twap.h"
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
};

class Engine {
public:
    static constexpr std::size_t kMaxOrders = 1000;
    static constexpr std::size_t kMaxOrderIdLength = 64;

    Engine(RiskLimits limits, std::size_t book_depth);

    bool apply_book_snapshot(const std::vector<Level>& bids, const std::vector<Level>& asks);
    bool apply_book_update(const std::vector<Level>& bids, const std::vector<Level>& asks);

    SubmitResult submit(const ParentOrderRequest& request);
    std::vector<Fill> step(std::int64_t now_ns);

    std::vector<OrderStatus> statuses() const;
    double position() const;
    std::optional<double> mid() const;

private:
    struct ParentOrder {
        ParentOrderRequest request;
        TwapSchedule schedule;
        OrderState state;
        double filled_qty;
        double filled_notional;
        double arrival_mid;
        double immediate_cost_bps;
        std::string halt_reason;
    };

    double projected_position_locked() const;

    mutable std::mutex mu_;
    OrderBook book_;
    RiskCheck risk_;
    double position_ = 0.0;
    std::vector<ParentOrder> orders_;
};

}  // namespace slipstream
