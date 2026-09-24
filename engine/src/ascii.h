#pragma once

// Locale-independent replacements for <cctype>: identifiers and numbers crossing the
// network boundary must be plain ASCII regardless of the process locale.
namespace slipstream {

constexpr bool is_ascii_digit(char c) { return c >= '0' && c <= '9'; }

constexpr bool is_ascii_upper(char c) { return c >= 'A' && c <= 'Z'; }

constexpr bool is_ascii_alnum(char c) {
    return is_ascii_digit(c) || is_ascii_upper(c) || (c >= 'a' && c <= 'z');
}

}  // namespace slipstream
