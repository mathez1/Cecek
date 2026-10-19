"""Does the picture say what the post says?

These are the tests for the class of bug that produces a confident, legible,
correctly-sized image asserting something the data does not. Nothing raises,
the suite stays green, and the wrong claim goes out over the account's name,
so the checks here have to look at geometry and pixels rather than at
whether a file appeared.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
import pytest

matplotlib.use("Agg")

import matplotlib.axes  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402

from bot import chart  # noqa: E402

FALLING = [(0, 100), (25, 56), (50, 30), (75, 14), (100, 5)]
RISING = [(x, x ** 3 / 10_000) for x in range(0, 101, 10)]

# The plot box in pixels, from the module's own layout constants.
WIDTH = int(chart.FIGSIZE[0] * chart.DPI)
HEIGHT = int(chart.FIGSIZE[1] * chart.DPI)
PLOT_BOX = (
    int(chart.LEFT * WIDTH),
    int((1 - chart.TOP) * HEIGHT),
    int(chart.RIGHT * WIDTH),
    int((1 - chart.BOTTOM) * HEIGHT),
)


@pytest.fixture
def plotted(monkeypatch):
    """Record what is handed to Axes.plot, in order."""
    calls = []
    real = matplotlib.axes.Axes.plot

    def spy(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(matplotlib.axes.Axes, "plot", spy)
    return calls


def a_line(path, points, **overrides):
    kwargs = dict(
        title="t", subtitle="", x_label="x", y_label="y",
        points=points, highlight=-1, highlight_label="", linear_reference=False,
    )
    kwargs.update(overrides)
    chart.draw_line(path, **kwargs)


class TestTheReferenceLine:
    """It is labelled "what you'd expect", so it has to be that line.

    Drawing it between the corners of the bounding box - min(xs) with
    min(ys), max with max - makes it rise whatever the data does. For a post
    correcting "efficiency should fall in proportion to load", the picture
    then asserted the opposite of the intuition being corrected.
    """

    def test_it_falls_with_a_falling_series(self, tmp_path, plotted):
        a_line(tmp_path / "p.png", FALLING, linear_reference=True)

        xs, ys = plotted[1][0], plotted[1][1]
        assert list(xs) == [0, 100]
        assert list(ys) == [100, 5], "the reference climbed while the data fell"

    def test_it_rises_with_a_rising_series(self, tmp_path, plotted):
        a_line(tmp_path / "p.png", RISING, linear_reference=True)

        xs, ys = plotted[1][0], plotted[1][1]
        assert list(xs) == [RISING[0][0], RISING[-1][0]]
        assert list(ys) == [RISING[0][1], RISING[-1][1]]

    def test_it_ends_where_the_series_ends_not_at_its_peak(self, tmp_path, plotted):
        humped = [(0, 10), (25, 70), (50, 95), (75, 60), (100, 12)]
        a_line(tmp_path / "p.png", humped, linear_reference=True)

        assert list(plotted[1][1]) == [10, 12]

    def test_the_label_lies_along_it(self):
        # Angled from the display transform, not the data slope: the plot box
        # is not square, so a 1:1 line reads at about 22 degrees, and a
        # falling one at about minus that.
        with plt.rc_context(chart._RC):
            fig, ax = plt.subplots(figsize=chart.FIGSIZE)
            ax.set_xlim(0, 100)
            ax.set_ylim(0, 100)

            chart._label_the_chord(ax, [0, 100], [0, 100])
            rising = ax.texts[-1].get_rotation()

            chart._label_the_chord(ax, [0, 100], [100, 0])
            falling = ax.texts[-1].get_rotation()
            plt.close(fig)

        # get_rotation normalises into [0, 360), so a downhill label comes
        # back as its positive complement.
        assert 0 < rising < 45
        assert 315 < falling < 360


class TestTheCallout:
    """Two lines of numbers that have to stay on the canvas.

    A fixed up-and-left offset walks off the axes whenever the marked point
    is the first or last one, which is often exactly the point worth marking.
    The text clipped to a letter or two and the post went out with it.
    """

    @pytest.mark.parametrize(
        "point",
        [(0, 0), (0, 100), (100, 0), (100, 100), (50, 50), (0, 50), (100, 50)],
        ids=["bl", "tl", "br", "tr", "middle", "left", "right"],
    )
    def test_the_text_anchor_stays_within_the_axes(self, point):
        with plt.rc_context(chart._RC):
            fig, ax = plt.subplots(figsize=chart.FIGSIZE)
            ax.set_xlim(0, 100)
            ax.set_ylim(0, 100)

            chart._callout(ax, point[0], point[1], "80% speed\n51% power")
            x, y = ax.texts[-1].xyann
            plt.close(fig)

        assert 0 <= x <= 100, f"the label anchor sits at x={x}, off the axes"
        assert 0 <= y <= 100, f"the label anchor sits at y={y}, off the axes"

    def test_it_still_points_at_the_marked_point(self):
        with plt.rc_context(chart._RC):
            fig, ax = plt.subplots(figsize=chart.FIGSIZE)
            ax.set_xlim(0, 100)
            ax.set_ylim(0, 100)

            chart._callout(ax, 80, 51.2, "80% speed\n51% power")
            annotation = ax.texts[-1]
            plt.close(fig)

        assert annotation.xy == (80, 51.2)


class TestBarsHaveTheRightHeights:
    """The one chart where a silently wrong result is easiest to produce.

    Swap or drop the heights argument and every bar renders at zero under a
    confident headline, with alt text stating numbers the picture does not
    show. The file is the right size, dark, and has a white title on it.
    """

    @staticmethod
    def _bar_tops(path: Path, bars: int) -> list[int]:
        """Topmost row holding bar-grey ink, per bar, inside the plot box."""
        with Image.open(path) as image:
            grey = image.convert("L").crop(PLOT_BOX)

        pixels = grey.load()
        width, height = grey.size
        tops = []
        for index in range(bars):
            left, right = index * width // bars, (index + 1) * width // bars
            top = height
            for row in range(height):
                # Bars are mid-grey. The near-black grid is below this band
                # and the white title and ink are above it.
                if any(40 <= pixels[col, row] <= 200 for col in range(left, right, 2)):
                    top = row
                    break
            tops.append(top)
        return tops

    def test_taller_values_reach_higher_up_the_canvas(self, tmp_path):
        path = tmp_path / "p.png"
        chart.draw_bar(
            path, title="t", subtitle="", x_label="x", y_label="y",
            categories=["a", "b", "c"], values=[3.0, 1.0, 2.0], highlight=-1,
        )

        a, b, c = self._bar_tops(path, 3)
        # A smaller row index is higher up the image.
        assert a < c < b, f"bar tops {a}, {b}, {c} do not rank 3 > 2 > 1"

    def test_the_heights_are_proportional_not_merely_ordered(self, tmp_path):
        path = tmp_path / "p.png"
        chart.draw_bar(
            path, title="t", subtitle="", x_label="x", y_label="y",
            categories=["a", "b"], values=[100.0, 50.0], highlight=-1,
        )

        tall, half = self._bar_tops(path, 2)
        with Image.open(path) as image:
            box_height = image.convert("L").crop(PLOT_BOX).size[1]

        # The short bar's height should be about half the tall one's.
        tall_height = box_height - tall
        half_height = box_height - half
        assert 0.42 < half_height / tall_height < 0.58
