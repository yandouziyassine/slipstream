from __future__ import annotations

from datetime import date

from slipstream.site.svg import bar_split, line_chart


def test_line_chart_with_no_series_shows_a_placeholder() -> None:
    svg = line_chart({})
    assert svg.startswith("<svg")
    assert "not enough data" in svg
    assert "<polyline" not in svg


def test_line_chart_with_one_point_draws_a_dot_not_a_line() -> None:
    svg = line_chart({"twap": [(date(2026, 9, 27), 40.5)]})
    assert "<circle" in svg
    assert "<polyline" not in svg


def test_line_chart_draws_one_polyline_per_series() -> None:
    series = {
        "twap": [(date(2026, 9, 25), 40.1), (date(2026, 9, 26), 40.2), (date(2026, 9, 27), 40.3)],
        "pov": [(date(2026, 9, 25), 39.9), (date(2026, 9, 26), 40.0), (date(2026, 9, 27), 40.1)],
    }
    svg = line_chart(series)
    assert svg.count("<polyline") == 2


def test_line_chart_draws_a_band_when_given_one() -> None:
    series = {"twap": [(date(2026, 9, 26), 40.2), (date(2026, 9, 27), 40.3)]}
    bands = {"twap": [(date(2026, 9, 26), 39.8, 40.6), (date(2026, 9, 27), 39.9, 40.7)]}
    svg = line_chart(series, bands)
    assert "<path" in svg


def test_line_chart_escapes_a_hostile_series_name() -> None:
    hostile = "<script>alert(1)</script>"
    svg = line_chart({hostile: [(date(2026, 9, 27), 40.0)]})
    assert "<script>alert" not in svg
    assert "&lt;script&gt;" in svg


def test_line_chart_only_valid_xml_svg_tag() -> None:
    svg = line_chart({"twap": [(date(2026, 9, 27), 40.0)]})
    assert svg.startswith("<svg viewBox=")
    assert svg.endswith("</svg>")


def test_bar_split_with_no_rows_shows_a_placeholder() -> None:
    svg = bar_split([])
    assert "not enough data" in svg


def test_bar_split_with_one_row_draws_two_rects() -> None:
    svg = bar_split([("twap", 0.98, 0.02)])
    assert svg.count("<rect") == 2


def test_bar_split_escapes_a_hostile_label() -> None:
    hostile = "<b>twap</b>"
    svg = bar_split([(hostile, 0.9, 0.1)])
    assert "<b>twap</b>" not in svg
    assert "&lt;b&gt;twap&lt;/b&gt;" in svg


def test_bar_split_zero_total_does_not_divide_by_zero() -> None:
    svg = bar_split([("twap", 0.0, 0.0)])
    assert "<svg" in svg
