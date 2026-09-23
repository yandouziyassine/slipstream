#include "config.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <stdexcept>
#include <string_view>

namespace slipstream {
namespace {

constexpr std::array<std::string_view, 3> kLoopbackPrefixes{"127.0.0.1:", "localhost:", "[::1]:"};

bool all_digits(const std::string& s) {
    return !s.empty() &&
           std::all_of(s.begin(), s.end(), [](unsigned char c) { return std::isdigit(c) != 0; });
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
    return std::all_of(symbol.begin(), symbol.end(), [](unsigned char c) {
        return std::isupper(c) != 0 || std::isdigit(c) != 0 || c == '/';
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

}  // namespace

ParseResult parse_args(const std::vector<std::string>& args) {
    EngineConfig config;
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
        } else {
            return {std::nullopt, "unknown flag " + flag};
        }
    }
    return {config, ""};
}

}  // namespace slipstream
