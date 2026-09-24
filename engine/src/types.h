#pragma once

namespace slipstream {

enum class Side { Buy, Sell };

struct Level {
    double price;
    double qty;
};

inline double signed_qty(Side side, double qty) { return side == Side::Buy ? qty : -qty; }

}  // namespace slipstream
