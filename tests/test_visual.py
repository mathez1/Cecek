"""The picture half: what the model asked for, and whether it can be drawn.

The governing rule these tests exist to protect is that a picture is a bonus.
Every malformed spec, every provider failure, every drawing bug has to end as
None and a text post, never as an exception that costs the run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bot import chart, visual


def line(**overrides):
    spec = {
        "kind": "line",
        "title": "Power runs on the cube of speed",
        "subtitle": "so 20% off the speed halves the electricity",
        "alt": "A curve rising slowly then steeply.",
        "x_label": "speed (%)",
        "y_label": "power (%)",
        "points": [{"x": i, "y": i ** 3 / 10_000} for i in range(0, 101, 5)],
        "linear_reference": True,
        "categories": [],
        "values": [],
        "highlight": 16,
        "highlight_label": "80% speed\n51% power",
        "figure": "",
        "caption": "",
        "prompt": "",
    }
    spec.update(overrides)
    return spec


def bar(**overrides):
    spec = line(
        kind="bar",
        points=[],
        linear_reference=False,
        categories=["a", "b", "c"],
        values=[3.0, 1.0, 2.0],
        highlight=0,
        highlight_label="",
    )
    spec.update(overrides)
    return spec


def stat(**overrides):
    spec = line(
        kind="stat",
        points=[],
        linear_reference=False,
        highlight=-1,
        highlight_label="",
        figure="340 ppm",
        caption="about 34 grains of the 10 grams",
    )
    spec.update(overrides)
    return spec


class TestParsing:
    def test_a_non_dict_is_no_picture(self):
        assert visual.parse(None).kind == "none"
        assert visual.parse("line").kind == "none"
        assert visual.parse([1, 2]).kind == "none"

    def test_an_unknown_kind_is_no_picture(self):
        assert visual.parse({"kind": "sankey"}).kind == "none"

    def test_points_that_are_not_finite_are_dropped(self):
        # A model computing a curve can land on an asymptote. matplotlib draws
        # inf as a silently broken line rather than raising.
        parsed = visual.parse(line(points=[
            {"x": 0, "y": 0},
            {"x": 1, "y": float("inf")},
            {"x": 2, "y": float("nan")},
            {"x": 3, "y": 9},
            {"x": "four", "y": 16},
        ]))
        assert parsed.points == [(0.0, 0.0), (3.0, 9.0)]

    def test_points_are_sorted_by_x(self):
        parsed = visual.parse(line(points=[
            {"x": 3, "y": 9}, {"x": 1, "y": 1}, {"x": 2, "y": 4},
        ]))
        assert [p[0] for p in parsed.points] == [1.0, 2.0, 3.0]

    def test_alt_text_is_capped_at_the_platform_limit(self):
        parsed = visual.parse(line(alt="x" * 4000))
        assert len(parsed.alt) == visual.ALT_LIMIT

    def test_a_nonsense_highlight_becomes_none(self):
        assert visual.parse(line(highlight="first")).highlight == -1

    def test_blank_categories_are_dropped(self):
        parsed = visual.parse(bar(categories=["a", "  ", "c"]))
        assert parsed.categories == ["a", "c"]


class TestWhatCannotBeDrawn:
    def test_a_good_line_is_drawable(self):
        assert visual.problem(visual.parse(line())) is None

    def test_too_few_points(self):
        spec = line(points=[{"x": 0, "y": 0}, {"x": 1, "y": 1}])
        assert "need 4" in visual.problem(visual.parse(spec))

    def test_too_many_points(self):
        spec = line(points=[{"x": i, "y": i} for i in range(visual.MAX_POINTS + 1)])
        assert "more than" in visual.problem(visual.parse(spec))

    def test_a_flat_line_is_not_a_relationship(self):
        spec = line(points=[{"x": i, "y": 5} for i in range(10)])
        assert "do not vary" in visual.problem(visual.parse(spec))

    def test_alt_text_is_required(self):
        assert visual.problem(visual.parse(line(alt=""))) == "no alt text"

    def test_bars_must_match_their_labels(self):
        spec = bar(categories=["a", "b", "c"], values=[1.0, 2.0])
        assert "against" in visual.problem(visual.parse(spec))

    def test_one_bar_is_not_a_comparison(self):
        assert "two bars" in visual.problem(visual.parse(bar(categories=["a"], values=[1.0])))

    def test_identical_bars_say_nothing(self):
        spec = bar(categories=["a", "b"], values=[2.0, 2.0])
        assert "same height" in visual.problem(visual.parse(spec))

    def test_a_stat_needs_a_figure(self):
        assert "no figure" in visual.problem(visual.parse(stat(figure="")))

    def test_an_illustration_needs_a_prompt(self):
        spec = stat(kind="illustration", figure="", caption="", prompt="")
        assert "no prompt" in visual.problem(visual.parse(spec))

    def test_none_is_not_an_error_worth_reporting(self):
        assert visual.problem(visual.Visual()) == "no visual requested"


class TestRendering:
    @pytest.mark.parametrize("spec", [line(), bar(), stat()], ids=["line", "bar", "stat"])
    def test_it_writes_a_png(self, spec, tmp_path):
        rendered = visual.render(visual.parse(spec), tmp_path)

        assert rendered is not None
        assert rendered.path.exists()
        assert rendered.path.read_bytes()[:4] == b"\x89PNG"
        assert rendered.alt

    @pytest.mark.parametrize("spec", [line(), bar(), stat()], ids=["line", "bar", "stat"])
    def test_the_canvas_is_dark_and_the_ink_is_not(self, spec, tmp_path):
        """A blank image raises nothing and passes a file-exists check.

        The style is applied through an rc context. If that context closes
        before savefig, or is never entered, every colour falls back to the
        matplotlib default: white ink on a white canvas. The PNG is the right
        size, has the right magic bytes, and shows nothing at all.
        """
        from PIL import Image

        rendered = visual.render(visual.parse(spec), tmp_path)

        with Image.open(rendered.path) as image:
            grey = image.convert("L")
            darkest, lightest = grey.getextrema()
            histogram = grey.histogram()

        assert darkest < 40, "nothing on this canvas is dark"
        assert lightest > 200, "there is no light ink on it"
        total = sum(histogram)
        average = sum(level * count for level, count in enumerate(histogram)) / total
        assert average < 64, f"the canvas averages {average:.0f}; it should be near black"

    def test_no_picture_asked_for(self, tmp_path):
        assert visual.render(visual.Visual(), tmp_path) is None

    def test_an_undrawable_spec_is_silent(self, tmp_path):
        spec = visual.parse(line(points=[{"x": 0, "y": 0}]))
        assert visual.render(spec, tmp_path) is None

    def test_a_drawing_bug_does_not_escape(self, tmp_path, monkeypatch):
        def explode(*args, **kwargs):
            raise ValueError("matplotlib had a bad day")

        monkeypatch.setattr(chart, "draw_line", explode)
        assert visual.render(visual.parse(line()), tmp_path) is None

    def test_an_empty_file_is_not_a_picture(self, tmp_path, monkeypatch):
        def touch(path, **kwargs):
            path.write_bytes(b"")

        monkeypatch.setattr(chart, "draw_stat", touch)
        assert visual.render(visual.parse(stat()), tmp_path) is None


class TestIllustrations:
    def _spec(self):
        return visual.parse(stat(
            kind="illustration", figure="", caption="",
            prompt="a bicycle leaning on a lamp post",
        ))

    def test_without_a_provider_there_is_no_illustration(self, tmp_path):
        assert visual.render(self._spec(), tmp_path) is None

    def test_a_provider_that_declines_loses_the_picture_not_the_post(self, tmp_path):
        assert visual.render(self._spec(), tmp_path, lambda prompt, path: False) is None

    def test_a_provider_that_delivers(self, tmp_path):
        def draw(prompt, path):
            assert "bicycle" in prompt
            path.write_bytes(b"\x89PNG" + b"0" * 50)
            return True

        rendered = visual.render(self._spec(), tmp_path, draw)
        assert rendered is not None
        assert rendered.kind == "illustration"

    def test_a_provider_that_raises_does_not_take_the_run_with_it(self, tmp_path):
        def draw(prompt, path):
            raise RuntimeError("vendor is down")

        assert visual.render(self._spec(), tmp_path, draw) is None
