"""Holding-period study charts: plain SVG text, no plotting dependency.

Two static figures for ``ops/studies``: the spread of walk-away after-tax XIRR by horizon (a box per
horizon, the NIFTY 500 TRI's median and D13's full-span return as reference marks), and the share of
windows meeting each hurdle by horizon (one line per hurdle, legend plus end labels). Figures are
ratios, never rupees, so floats are fine here. Colours are the reference categorical slots in fixed
order; text wears text tokens, never a series colour.

What it never does: read the lake, or draw a second y-axis.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Final

__all__ = ["horizon_box_svg", "probability_svg"]

_SURFACE: Final = "#fcfcfb"
_TEXT: Final = "#0b0b0b"
_TEXT_2: Final = "#52514e"
_GRID: Final = "#e4e3df"
_SERIES: Final = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6")
_W: Final = 960
_H: Final = 520
_LEFT: Final = 72
_RIGHT: Final = 230
_TOP: Final = 56
_BOTTOM: Final = 64


def _frame(title: str, subtitle: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
            f'viewBox="0 0 {_W} {_H}" font-family="system-ui, -apple-system, sans-serif">',
            f'<rect width="{_W}" height="{_H}" fill="{_SURFACE}"/>',
            f'<text x="{_LEFT}" y="24" font-size="16" font-weight="600" fill="{_TEXT}">'
            f"{escape(title)}</text>",
            f'<text x="{_LEFT}" y="42" font-size="12" fill="{_TEXT_2}">{escape(subtitle)}</text>',
            *body,
            "</svg>",
            "",
        ]
    )


def _y_axis(lo: float, hi: float, step: float, fmt: str) -> tuple[list[str], float, float]:
    """Gridlines and labels; returns them and the (lo, hi) the plot maps to."""
    plot_h = _H - _TOP - _BOTTOM
    out: list[str] = []
    value = lo
    while value <= hi + 1e-9:
        y = _TOP + plot_h * (1 - (value - lo) / (hi - lo))
        out.append(
            f'<line x1="{_LEFT}" x2="{_W - _RIGHT}" y1="{y:.1f}" y2="{y:.1f}" stroke="{_GRID}" '
            f'stroke-width="{2 if abs(value) < 1e-9 else 1}"/>'
        )
        out.append(
            f'<text x="{_LEFT - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" '
            f'fill="{_TEXT_2}">{fmt.format(value)}</text>'
        )
        value += step
    return out, lo, hi


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def horizon_box_svg(
    series: Mapping[int, Sequence[float]],
    tri_median: Mapping[int, float],
    expected: float,
    *,
    title: str,
) -> str:
    """A box (10th-90th pct whiskers, quartile box, median) per horizon, with reference marks."""
    horizons = sorted(series)
    every = [v for h in horizons for v in series[h]]
    lo = max(-0.6, (min(every) // 0.1) * 0.1) if every else -0.2
    hi = min(1.5, (max(every) // 0.1 + 1) * 0.1) if every else 0.5
    clip_lo, clip_hi = lo, hi
    grid, lo, hi = _y_axis(lo, hi, 0.1 if hi - lo <= 1.2 else 0.2, "{:.0%}")
    plot_w = _W - _LEFT - _RIGHT
    plot_h = _H - _TOP - _BOTTOM

    def y(v: float) -> float:
        v = min(max(v, clip_lo), clip_hi)
        return _TOP + plot_h * (1 - (v - lo) / (hi - lo))

    body = list(grid)
    slot = plot_w / max(len(horizons), 1)
    for i, h in enumerate(horizons):
        values = series[h]
        cx = _LEFT + slot * (i + 0.5)
        p10, p25, p50, p75, p90 = (_quantile(values, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9))
        half = min(18.0, slot * 0.3)
        body += [
            f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(p90):.1f}" y2="{y(p10):.1f}" '
            f'stroke="{_SERIES[0]}" stroke-width="2"/>',
            f'<rect x="{cx - half:.1f}" y="{y(p75):.1f}" width="{2 * half:.1f}" '
            f'height="{max(y(p25) - y(p75), 1):.1f}" rx="4" fill="{_SERIES[0]}" '
            'fill-opacity="0.35" stroke="#2a78d6" stroke-width="2"/>',
            f'<line x1="{cx - half:.1f}" x2="{cx + half:.1f}" y1="{y(p50):.1f}" '
            f'y2="{y(p50):.1f}" stroke="{_TEXT}" stroke-width="2"/>',
            f'<circle cx="{cx:.1f}" cy="{y(min(values)):.1f}" r="4" fill="{_SERIES[1]}"/>',
            f'<text x="{cx:.1f}" y="{_H - _BOTTOM + 18}" font-size="11" text-anchor="middle" '
            f'fill="{_TEXT_2}">{h} m</text>',
            f'<text x="{cx:.1f}" y="{_H - _BOTTOM + 32}" font-size="10" text-anchor="middle" '
            f'fill="{_TEXT_2}">n={len(values)}</text>',
        ]
        if h in tri_median:
            ty = y(tri_median[h])
            body.append(
                f'<path d="M{cx - 6:.1f},{ty:.1f} l6,-6 l6,6 l-6,6 z" fill="{_SERIES[3]}" '
                f'stroke="{_SURFACE}" stroke-width="2"/>'
            )
    ey = y(expected)
    body += [
        f'<line x1="{_LEFT}" x2="{_W - _RIGHT}" y1="{ey:.1f}" y2="{ey:.1f}" stroke="{_SERIES[5]}" '
        'stroke-width="2" stroke-dasharray="6 4"/>',
        f'<text x="{_W - _RIGHT + 8}" y="{ey + 4:.1f}" font-size="11" fill="{_TEXT}">'
        f"E = {expected:.1%} (full span)</text>",
        f'<text x="{_LEFT + plot_w / 2:.1f}" y="{_H - 12}" font-size="12" text-anchor="middle" '
        f'fill="{_TEXT_2}">holding period (months from a month-start fresh entry)</text>',
    ]
    legend_x = _W - _RIGHT + 8
    body += [
        f'<rect x="{legend_x}" y="{_TOP}" width="14" height="10" rx="2" fill="{_SERIES[0]}" '
        'fill-opacity="0.35" stroke="#2a78d6"/>',
        f'<text x="{legend_x + 20}" y="{_TOP + 9}" font-size="11" fill="{_TEXT}">'
        "D13: 25th-75th pct, median</text>",
        f'<line x1="{legend_x + 7}" x2="{legend_x + 7}" y1="{_TOP + 18}" y2="{_TOP + 32}" '
        f'stroke="{_SERIES[0]}" stroke-width="2"/>',
        f'<text x="{legend_x + 20}" y="{_TOP + 29}" font-size="11" fill="{_TEXT}">'
        "D13: 10th-90th pct</text>",
        f'<circle cx="{legend_x + 7}" cy="{_TOP + 44}" r="4" fill="{_SERIES[1]}"/>',
        f'<text x="{legend_x + 20}" y="{_TOP + 48}" font-size="11" fill="{_TEXT}">'
        "D13: worst window</text>",
        f'<path d="M{legend_x + 1},{_TOP + 62} l6,-6 l6,6 l-6,6 z" fill="{_SERIES[3]}"/>',
        f'<text x="{legend_x + 20}" y="{_TOP + 66}" font-size="11" fill="{_TEXT}">'
        "NIFTY 500 TRI: median</text>",
    ]
    return _frame(
        f"{title}: walk-away after-tax XIRR by holding period",
        "Every month-start fresh entry, ₹10 crore/day floor; values clipped to the axis range",
        body,
    )


def probability_svg(curves: Mapping[str, Sequence[tuple[int, float]]], *, title: str) -> str:
    """P(window meets hurdle) by horizon, one line per hurdle; x is evenly spaced by horizon."""
    horizons = sorted({h for points in curves.values() for h, _ in points})
    grid, lo, hi = _y_axis(0.0, 1.0, 0.1, "{:.0%}")
    plot_w = _W - _LEFT - _RIGHT
    plot_h = _H - _TOP - _BOTTOM
    slot = plot_w / max(len(horizons) - 1, 1)
    xs = {h: _LEFT + slot * i for i, h in enumerate(horizons)}

    def y(v: float) -> float:
        return _TOP + plot_h * (1 - (v - lo) / (hi - lo))

    body = list(grid)
    for level in (0.75, 0.9):
        body.append(
            f'<text x="{_LEFT + 4}" y="{y(level) - 4:.1f}" font-size="10" fill="{_TEXT_2}">'
            f"{level:.0%} bar</text>"
        )
    for h in horizons:
        body.append(
            f'<text x="{xs[h]:.1f}" y="{_H - _BOTTOM + 18}" font-size="11" text-anchor="middle" '
            f'fill="{_TEXT_2}">{h} m</text>'
        )
    for i, (name, points) in enumerate(curves.items()):
        colour = _SERIES[i % len(_SERIES)]
        path = " ".join(
            f"{'M' if j == 0 else 'L'}{xs[h]:.1f},{y(p):.1f}" for j, (h, p) in enumerate(points)
        )
        body.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="2"/>')
        for h, p in points:
            body.append(
                f'<circle cx="{xs[h]:.1f}" cy="{y(p):.1f}" r="4" fill="{colour}" '
                f'stroke="{_SURFACE}" stroke-width="2"/>'
            )
        ly = _TOP + 8 + 18 * i
        body += [
            f'<line x1="{_W - _RIGHT + 8}" x2="{_W - _RIGHT + 24}" y1="{ly:.1f}" y2="{ly:.1f}" '
            f'stroke="{colour}" stroke-width="2"/>',
            f'<text x="{_W - _RIGHT + 30}" y="{ly + 4:.1f}" font-size="11" fill="{_TEXT}">'
            f"{escape(name[:30])}</text>",
        ]
    body.append(
        f'<text x="{_LEFT + plot_w / 2:.1f}" y="{_H - 12}" font-size="12" text-anchor="middle" '
        f'fill="{_TEXT_2}">holding period (months; not to scale)</text>'
    )
    return _frame(
        f"{title}: share of windows meeting each hurdle",
        "Walk-away after-tax XIRR against the hurdle over the same window, month-start grid",
        body,
    )
