"""Drawing. Three shapes, one ink scale, no colour.

A post that already states a number does not need an illustration of the
number; it needs the number drawn correctly. That is the whole argument for
this module existing instead of a second image vendor.

Monochrome is not only an aesthetic match for the avatar. One series means
there is no categorical palette to get wrong, no colourblind-safety question
to answer, and nothing that reads differently on someone else's screen.

Everything here is deterministic: the same spec renders the same PNG. Nothing
is fetched, nothing is sampled, nothing costs money.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

# Must precede pyplot. A GitHub runner has no display, and the default
# interactive backend would fail at import rather than at draw time, which
# turns a cosmetic feature into a dead run.
matplotlib.use("Agg")

import matplotlib.patheffects as patheffects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger(__name__)

# X renders feed images at 16:9. Anything taller gets cropped top and bottom,
# which is where titles live.
FIGSIZE = (8.0, 4.5)
DPI = 200

SURFACE = "#0B0B0B"
INK = "#FFFFFF"
INK_2 = "#9A9A9A"
INK_MUTED = "#4E4E4E"
GRID = "#1C1C1C"

# Labels sit wherever the data leaves room, which is sometimes on top of the
# curve. A stroke in the surface colour around the glyphs keeps them readable
# there without having to solve label placement properly.
HALO = [patheffects.withStroke(linewidth=3.5, foreground=SURFACE)]

# Plot box as a fraction of the figure, shared by the two axes-based shapes so
# the reference-line label can be angled correctly (see _reference_angle).
LEFT, RIGHT, TOP, BOTTOM = 0.085, 0.97, 0.78, 0.145

# Applied through rc_context around each draw rather than written into the
# global rcParams, so importing this module cannot change the look of anyone
# else's plots in the same process.
#
# It has to wrap savefig too, not just the plotting calls: text and face
# colours are resolved when the figure is rendered, so a context that closes
# before the save produces a white canvas with white ink on it, which is to
# say a blank image that raises nothing.
_RC = {
    "font.family": "DejaVu Sans",  # ships with matplotlib; no system font hunt
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "text.color": INK,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK_2,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
}


def _figure():
    fig = plt.figure(figsize=FIGSIZE, dpi=DPI)
    fig.text(0.985, 0.04, "@bob_thinks", ha="right", va="bottom",
             fontsize=8, color=INK_MUTED)
    return fig


def _headings(fig, title: str, subtitle: str) -> None:
    if title:
        fig.text(LEFT, 0.925, title, fontsize=15.5, color=INK,
                 ha="left", va="top")
    if subtitle:
        fig.text(LEFT, 0.845, subtitle, fontsize=9.5, color=INK_2,
                 ha="left", va="top")


def _dress(ax, x_label: str, y_label: str) -> None:
    """Recessive chrome: hairline grid, no tick marks, two spines."""
    ax.set_xlabel(x_label, fontsize=9.5, labelpad=7)
    ax.set_ylabel(y_label, fontsize=9.5, labelpad=7)
    ax.tick_params(length=0, labelsize=9, pad=6)
    ax.grid(True, color=GRID, linewidth=0.7, zorder=1)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(0.8)


def _label_the_chord(ax, xs: list[float], ys: list[float]) -> None:
    """Name the reference line, lying along it at whatever angle it is drawn.

    The angle comes from the display transform rather than from the data,
    because the plot box is not square: a 1:1 line reads at about 22 degrees
    here, and a falling one at minus that. Labelling it from the data slope
    puts the words across the line instead of along it.
    """
    from math import atan2, degrees

    start = ax.transData.transform((xs[0], ys[0]))
    end = ax.transData.transform((xs[-1], ys[-1]))
    angle = degrees(atan2(end[1] - start[1], end[0] - start[0]))
    # Keep it readable rather than upside down if the points ever arrive
    # right to left.
    if angle > 90 or angle < -90:
        angle += 180

    ax.text(
        xs[0] + (xs[-1] - xs[0]) * 0.46,
        ys[0] + (ys[-1] - ys[0]) * 0.46,
        "what you'd expect",
        color=INK_MUTED, fontsize=8.5, ha="center", va="bottom",
        rotation=angle, rotation_mode="anchor", zorder=2,
        path_effects=HALO,
    )


def _callout(ax, hx: float, hy: float, label: str) -> None:
    """Two lines of numbers beside the marked point, kept on the canvas.

    A fixed up-and-left offset walks off the axes whenever the marked point
    is the first or the last one, which is often exactly the point worth
    marking. The text clipped to a letter or two and the post went out with
    it, because nothing about that raises.
    """
    left, right = ax.get_xlim()
    bottom, top = ax.get_ylim()
    width, height = right - left, top - bottom

    # Lean away from the nearer edge on each axis.
    going_left = hx > left + width * 0.5
    going_down = hy > bottom + height * 0.62

    text_x = hx + (-1 if going_left else 1) * width * 0.14
    text_y = hy + (-1 if going_down else 1) * height * 0.27

    # Then keep the anchor inside, with room for two lines of text.
    margin_x, margin_y = width * 0.03, height * 0.12
    text_x = min(max(text_x, left + margin_x), right - margin_x)
    text_y = min(max(text_y, bottom + margin_y), top - margin_y)

    ax.annotate(
        label,
        xy=(hx, hy),
        xytext=(text_x, text_y),
        color=INK, fontsize=10.5, linespacing=1.35,
        ha="right" if going_left else "left",
        va="top" if going_down else "bottom",
        arrowprops=dict(arrowstyle="-", color=INK_MUTED, linewidth=1.0,
                        shrinkA=2, shrinkB=7),
        zorder=5,
        path_effects=HALO,
    )


def draw_line(
    path: Path,
    *,
    title: str,
    subtitle: str,
    x_label: str,
    y_label: str,
    points: list[tuple[float, float]],
    highlight: int = -1,
    highlight_label: str = "",
    linear_reference: bool = False,
) -> None:
    """One curve, optionally against the straight line you'd have guessed."""
    with plt.rc_context(_RC):
        fig = _figure()
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=LEFT, right=RIGHT, top=TOP, bottom=BOTTOM)

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]

        ax.plot(xs, ys, color=INK, linewidth=2.2, solid_capstyle="round", zorder=3)

        if linear_reference:
            # The chord between the two ENDPOINTS, not between the corners of
            # the bounding box. Taking min(xs) with min(ys) draws a rising
            # line whatever the data does, so for a falling series it asserted
            # the opposite of the intuition the post was correcting.
            ax.plot(
                [xs[0], xs[-1]], [ys[0], ys[-1]],
                color=INK_MUTED, linewidth=1.0, zorder=2,
            )
            _label_the_chord(ax, xs, ys)

        if 0 <= highlight < len(points):
            hx, hy = points[highlight]
            # The surface-coloured ring is what keeps the marker readable where it
            # sits on top of the curve.
            ax.plot([hx], [hy], marker="o", markersize=9, color=INK,
                    markeredgecolor=SURFACE, markeredgewidth=2.5, zorder=4)
            if highlight_label:
                _callout(ax, hx, hy, highlight_label)

        _dress(ax, x_label, y_label)
        _headings(fig, title, subtitle)
        fig.savefig(path)
        plt.close(fig)


def draw_bar(
    path: Path,
    *,
    title: str,
    subtitle: str,
    x_label: str,
    y_label: str,
    categories: list[str],
    values: list[float],
    highlight: int = -1,
) -> None:
    """Magnitude across a handful of named things."""
    with plt.rc_context(_RC):
        fig = _figure()
        ax = fig.add_subplot(111)
        fig.subplots_adjust(left=LEFT, right=RIGHT, top=TOP, bottom=BOTTOM)

        positions = range(len(categories))
        # One series, one colour. Shading bars by their own height would spend the
        # only free channel re-encoding what the bar length already says.
        bars = ax.bar(positions, values, color=INK_MUTED, width=0.62, zorder=3)
        if 0 <= highlight < len(bars):
            bars[highlight].set_color(INK)

        ax.set_xticks(list(positions))
        ax.set_xticklabels(categories, fontsize=9)
        _dress(ax, x_label, y_label)
        ax.grid(axis="x", visible=False)  # vertical rules add nothing to bars
        _headings(fig, title, subtitle)
        fig.savefig(path)
        plt.close(fig)


def draw_stat(
    path: Path,
    *,
    title: str,
    figure_text: str,
    subtitle: str,
    caption: str,
) -> None:
    """One number, no plot.

    A single value has no shape, and a one-bar bar chart is a worse way of
    saying it than simply saying it large. Read top to bottom the three lines
    have to form a sentence.
    """
    with plt.rc_context(_RC):
        fig = _figure()
        if title:
            fig.text(0.5, 0.75, title.upper(), fontsize=10.5, color=INK_2,
                     ha="center", va="center")
        # Long figures need to shrink or they run off the canvas.
        size = 62 if len(figure_text) <= 9 else max(34, int(62 * 9 / len(figure_text)))
        fig.text(0.5, 0.535, figure_text, fontsize=size, color=INK,
                 ha="center", va="center")
        if subtitle:
            fig.text(0.5, 0.395, subtitle, fontsize=13, color=INK_2,
                     ha="center", va="center")
        if caption:
            fig.text(0.5, 0.215, caption, fontsize=11, color=INK_2,
                     ha="center", va="center", linespacing=1.6)
        fig.savefig(path)
        plt.close(fig)
