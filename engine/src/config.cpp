#include "config.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <stdexcept>
#include <string_view>

#include "ascii.h"

namespace slipstream {
namespace {

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

std::optional<VenueConfig> parse_venue(const std::string& value) {
    const auto colon = value.find(':');
    if (colon == std::string::npos) return std::nullopt;
    const std::string name = value.substr(0, colon);
    if (!is_known_venue(name)) return std::nullopt;
    const std::string rest = value.substr(colon + 1);
    constexpr std::string_view prefix = "fee_bps=";
    if (rest.size() < prefix.size() || rest.compare(0, prefix.size(), prefix) != 0) return std::nullopt;
    const std::string number = rest.substr(prefix.size());
    const auto dots = std::count(number.begin(), number.end(), '.');
    const bool plain_decimal =
        dots <= 1 && number.find_first_not_of("0123456789.") == std::string::npos &&
        number.find_first_of("0123456789") != std::string::npos;
    if (!plain_decimal) return std::nullopt;
    try {
        std::size_t consumed = 0;
        const double fee_bps = std::stod(number, &consumed);
        if (consumed != number.size() || !std::isfinite(fee_bps) || fee_bps < 0.0 || fee_bps > 1000.0) {
            return std::nullopt;
        }
        return VenueConfig{name, fee_bps};
    } catch (const std::exception&) {
        return std::nullopt;
    }
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
                        "invalid --venue (name:fee_bps=N, name in kraken|coinbase, 0<=N<=1000)"};
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
        } else if (flag == "--stale-ms") {
            const auto parsed = parse_stale_ms(value);
            if (!parsed) return {std::nullopt, "invalid --stale-ms (1-600000)"};
            config.stale_ns = *parsed;
        } else {
            return {std::nullopt, "unknown flag " + flag};
        }
    }
    return {config, ""};
}

}  // namespace slipstream
