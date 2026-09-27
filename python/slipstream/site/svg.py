from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from slipstream.site.escaping import esc

_MARGIN_LEFT = 40
_MARGIN_RIGHT = 10
_MARGIN_TOP = 10
_MARGIN_BOTTOM = 28
_LINE_COLORS = ("#1f6b47", "#1b1b1a", "#6b6a64", "#1f4e79")
_BAND_COLOR = "#1f6b47"
_FEE_COLOR = "#1b1b1a"
_SLIPPAGE_COLOR = "#a3401f"

Point = tuple[date, float]
BandPoint = tuple[date, float, float]


def _placeholder(width: int, height: int, label: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{esc(label)}">'
        f'<text x="{_MARGIN_LEFT}" y="{height // 2}">not enough data yet</text></svg>'
    )


def line_chart(
    series: Mapping[str, Sequence[Point]],
    bands: Mapping[str, Sequence[BandPoint]] | None = None,
    width: int = 560,
    height: int = 220,
) -> str:
    """A line per series, over a shared date axis, with an optional shaded 95% band per series.
    Only numeric values reach the geometry; every label is escaped."""
    bands = bands or {}
    all_dates = sorted({point[0] for points in series.values() for point in points})
    all_values = [point[1] for points in series.values() for point in points]
    for band_points in bands.values():
        for _, lo, hi in band_points:
            all_values.extend((lo, hi))
    if not all_dates or not all_values:
        return _placeholder(width, height, "not enough data yet")

    y_min, y_max = min(all_values), max(all_values)
    if y_min == y_max:
        y_min, y_max = y_min - 1.0, y_max + 1.0
    span = max(len(all_dates) - 1, 1)
    x_index = {d: i for i, d in enumerate(all_dates)}
    plot_w = width - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = height - _MARGIN_TOP - _MARGIN_BOTTOM

    def x_of(d: date) -> float:
        return _MARGIN_LEFT + (x_index[d] / span) * plot_w

    def y_of(v: float) -> float:
        return _MARGIN_TOP + plot_h - (v - y_min) / (y_max - y_min) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="Cost by strategy over time">'
    ]
    for band_points in bands.values():
        if len(band_points) < 2:
            continue
        top = [(x_of(d), y_of(hi)) for d, _, hi in band_points]
        bottom = [(x_of(d), y_of(lo)) for d, lo, _ in reversed(band_points)]
        path = " L ".join(f"{x:.1f},{y:.1f}" for x, y in (*top, *bottom))
        parts.append(f'<path d="M {path} Z" fill="{_BAND_COLOR}" opacity="0.12"/>')

    for i, (name, points) in enumerate(series.items()):
        color = _LINE_COLORS[i % len(_LINE_COLORS)]
        if len(points) == 1:
            x, y = x_of(points[0][0]), y_of(points[0][1])
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{color}"/>')
        else:
            coords = " ".join(f"{x_of(d):.1f},{y_of(v):.1f}" for d, v in points)
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{coords}"/>'
            )
        last_d, last_v = points[-1]
        parts.append(
            f'<text x="{x_of(last_d) - 4:.1f}" y="{y_of(last_v) - 6:.1f}" '
            f'style="fill:{color}">{esc(name)}</text>'
        )
    axis_y = height - _MARGIN_BOTTOM + 14
    parts.append(f'<text x="{_MARGIN_LEFT}" y="{axis_y}">{esc(all_dates[0].isoformat())}</text>')
    parts.append(f'<text x="{width - 70}" y="{axis_y}">{esc(all_dates[-1].isoformat())}</text>')
    parts.append("</svg>")
    return "".join(parts)


def bar_split(
    rows: Sequence[tuple[str, float, float]], width: int = 360, row_height: int = 30
) -> str:
    """One horizontal fee/slippage split bar per row. Only numeric fractions reach the
    geometry; every label is escaped."""
    if not rows:
        return _placeholder(width, 40, "not enough data yet")
    bar_x = 60
    bar_w = width - bar_x - 20
    height = len(rows) * row_height + 10
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="Fees versus slippage">'
    ]
    for i, (label, fee_frac, slippage_frac) in enumerate(rows):
        total = fee_frac + slippage_frac
        fee_w = bar_w * (fee_frac / total) if total > 0 else 0.0
        slippage_w = bar_w * (slippage_frac / total) if total > 0 else 0.0
        y = 10 + i * row_height
        parts.append(f'<text x="0" y="{y + 12}">{esc(label)}</text>')
        parts.append(
            f'<rect x="{bar_x}" y="{y}" width="{fee_w:.1f}" height="16" fill="{_FEE_COLOR}"/>'
        )
        parts.append(
            f'<rect x="{bar_x + fee_w:.1f}" y="{y}" width="{slippage_w:.1f}" height="16" '
            f'fill="{_SLIPPAGE_COLOR}"/>'
        )
    parts.append("</svg>")
    return "".join(parts)
