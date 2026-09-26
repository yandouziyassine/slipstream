#include "config.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <string_view>

#include "ascii.h"

namespace slipstream {
namespace {

constexpr double kMaxDeviationBps = 10000.0;
constexpr std::array<std::string_view, 3> kLoopbackPrefixes{"127.0.0.1:", "localhost:", "[::1]:"};

bool all_digits(const std::string& s) {
    return !s.empty() &&
           std::all_of(s.begin(), s.end(), [](char c) { return is_ascii_digit(c); });
}

bool is_loopback(const std::string& address) {
    for (const auto prefix : kLoopbackPrefixes) {
        if (!address.starts_with(prefix)) continue;
        const std::string port = address.substr(prefix.size());
        return all_digits(port) && port.size() <= 5 && std::stoul(port) <= 65535;
    }
    return false;
}

bool valid_symbol(const std::string& symbol) {
    if (symbol.empty() || symbol.size() > 32) return false;
    return std::all_of(symbol.begin(), symbol.end(), [](char c) {
        return is_ascii_upper(c) || is_ascii_digit(c) || c == '/';
    });
}

std::optional<double> parse_positive(const std::string& text) {
    try {
        std::size_t consumed = 0;
        const double value = std::stod(text, &consumed);
        if (consumed != text.size() || !std::isfinite(value) || value <= 0.0) return std::nullopt;
        return value;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

std::optional<std::size_t> parse_depth(const std::string& text) {
    if (!all_digits(text) || text.size() > 4) return std::nullopt;
    const auto value = std::stoul(text);
    if (value < 1 || value > 1000) return std::nullopt;
    return value;
}

bool is_known_venue(std::string_view name) {
    return std::find(kKnownVenues.begin(), kKnownVenues.end(), name) != kKnownVenues.end();
}

// Plain decimal only: no sign, exponent, whitespace, nan or inf.
std::optional<double> parse_plain_decimal(std::string_view text, double max) {
    const auto dots = std::count(text.begin(), text.end(), '.');
    const bool plain_decimal = dots <= 1 &&
                               text.find_first_not_of("0123456789.") == std::string_view::npos &&
                               text.find_first_of("0123456789") != std::string_view::npos;
    if (!plain_decimal) return std::nullopt;
    try {
        const std::string number(text);
        std::size_t consumed = 0;
        const double value = std::stod(number, &consumed);
        if (consumed != number.size() || !std::isfinite(value) || value < 0.0 || value > max) {
            return std::nullopt;
        }
        return value;
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

struct VenueKey {
    std::string_view name;
    double max;
    double VenueConfig::*field;
};

// fee_bps first: it is the only required key.
constexpr std::array<VenueKey, 4> kVenueKeys{{{"fee_bps", 1000.0, &VenueConfig::fee_bps},
                                              {"min_qty", 1e6, &VenueConfig::min_qty},
                                              {"qty_step", 1e6, &VenueConfig::qty_step},
                                              {"min_notional", 1e9, &VenueConfig::min_notional}}};

std::optional<VenueConfig> parse_venue(const std::string& value) {
    const auto colon = value.find(':');
    if (colon == std::string::npos) return std::nullopt;
    VenueConfig venue{value.substr(0, colon), 0.0};
    if (!is_known_venue(venue.name)) return std::nullopt;
    std::array<bool, kVenueKeys.size()> seen{};
    std::string_view rest = std::string_view(value).substr(colon + 1);
    while (true) {
        const auto comma = rest.find(',');
        const std::string_view item = rest.substr(0, comma);
        const auto equals = item.find('=');
        if (equals == std::string_view::npos) return std::nullopt;
        const auto key = std::find_if(kVenueKeys.begin(), kVenueKeys.end(), [&](const VenueKey& k) {
            return k.name == item.substr(0, equals);
        });
        if (key == kVenueKeys.end()) return std::nullopt;
        auto& key_seen = seen[static_cast<std::size_t>(key - kVenueKeys.begin())];
        if (key_seen) return std::nullopt;
        key_seen = true;
        const auto number = parse_plain_decimal(item.substr(equals + 1), key->max);
        if (!number) return std::nullopt;
        venue.*(key->field) = *number;
        if (comma == std::string_view::npos) break;
        rest = rest.substr(comma + 1);
    }
    if (!seen[0]) return std::nullopt;
    return venue;
}

std::optional<std::int64_t> parse_stale_ms(const std::string& text) {
    if (!all_digits(text) || text.size() > 6) return std::nullopt;
    const auto value = std::stoul(text);
    if (value < 1 || value > 600000) return std::nullopt;
    return static_cast<std::int64_t>(value) * 1'000'000;
}

}  // namespace

ParseResult parse_args(const std::vector<std::string>& args) {
    EngineConfig config;
    bool venue_flag_seen = false;
    for (std::size_t i = 0; i < args.size(); i += 2) {
        const std::string& flag = args[i];
        if (i + 1 >= args.size()) return {std::nullopt, "missing value for " + flag};
        const std::string& value = args[i + 1];

        if (flag == "--listen") {
            if (!is_loopback(value)) return {std::nullopt, "--listen must be a loopback host:port"};
            config.listen_address = value;
        } else if (flag == "--symbol") {
            if (!valid_symbol(value)) return {std::nullopt, "invalid --symbol"};
            config.symbol = value;
        } else if (flag == "--max-order-notional") {
            const auto parsed = parse_positive(value);
            if (!parsed) return {std::nullopt, "invalid --max-order-notional"};
            config.limits.max_order_notional = *parsed;
        } else if (flag == "--max-position") {
            const auto parsed = parse_positive(value);
            if (!parsed) return {std::nullopt, "invalid --max-position"};
            config.limits.max_abs_position = *parsed;
        } else if (flag == "--book-depth") {
            const auto parsed = parse_depth(value);
            if (!parsed) return {std::nullopt, "invalid --book-depth (1-1000)"};
            config.book_depth = *parsed;
        } else if (flag == "--venue") {
            const auto parsed = parse_venue(value);
            if (!parsed) {
                return {std::nullopt,
                        "invalid --venue (name:fee_bps=F[,min_qty=Q][,qty_step=S][,min_notional=N]"
                        ", name in kraken|coinbase, plain decimals, 0<=F<=1000)"};
            }
            if (!venue_flag_seen) {
                config.venues.clear();
                venue_flag_seen = true;
            }
            const bool duplicate = std::any_of(
                config.venues.begin(), config.venues.end(),
                [&](const VenueConfig& registered) { return registered.name == parsed->name; });
            if (duplicate) return {std::nullopt, "duplicate --venue"};
            config.venues.push_back(*parsed);
        } else if (flag == "--max-deviation-bps") {
            const auto parsed = parse_plain_decimal(value, kMaxDeviationBps);
            if (!parsed || *parsed <= 0.0) {
                return {std::nullopt, "invalid --max-deviation-bps (plain decimal, 0<N<=10000)"};
            }
            config.max_deviation_bps = *parsed;
        } else if (flag == "--stale-ms") {
            const auto parsed = parse_stale_ms(value);
            if (!parsed) return {std::nullopt, "invalid --stale-ms (1-600000)"};
            config.stale_ns = *parsed;
        } else if (flag == "--clock") {
            if (value == "live") {
                config.clock = ClockMode::Live;
            } else if (value == "replay") {
                config.clock = ClockMode::Replay;
            } else {
                return {std::nullopt, "invalid --clock (must be live or replay)"};
            }
        } else {
            return {std::nullopt, "unknown flag " + flag};
        }
    }
    return {config, ""};
}

}  // namespace slipstream
