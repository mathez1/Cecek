"""Whether a post gets a picture, and which kind.

The model decides. This module only states the choices, checks the answer is
mechanically drawable, and dispatches. It never second-guesses the judgement,
for the same reason guard.py never argues with the idea of a post.

The ordering principle: a picture is a bonus. Nothing here may cost a post.
Every failure path returns None and the post goes out as text, because a
chart that will not render is a far smaller loss than a run that produces
nothing.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# "none" is still a value parse() can return, for an answer it cannot read,
# but it is never offered to the model: every post gets a picture. Given the
# option, the model took it on six posts out of six.
KINDS = ("none", "line", "bar", "stat", "card", "illustration")

# A headline long enough to need a fourth line has stopped being a headline.
CARD_HEADLINE_LIMIT = 120

# X truncates alt text past 1000 characters.
ALT_LIMIT = 1000

# Enough points to read as a curve rather than a polygon, few enough that a
# model padding the array cannot blow up the response.
MIN_POINTS = 4
MAX_POINTS = 400

# Past about eight bars the labels collide at feed size.
MAX_BARS = 8

# Two framings of the same question, because what the model should reach
# for first depends on whether this run can draw from imagination at all.
#
# With an image key, the illustration is the default: the owner wants the
# picture to be what BoB sees when he thinks about the subject, not a
# readout of it. Charts stay available for the post that is nothing but a
# number. Without a key, charts and title cards are all there is.
DESCRIBE_WITH_ILLUSTRATION = (
    "The picture that goes with this post. Every post gets one. Choose "
    "'illustration' for nearly every post: a drawing of what you see in "
    "your head when you think about this, literal or a visual metaphor, "
    "your own idea of it rather than a diagram. Choose 'line', 'bar' or "
    "'stat' only when the post is entirely about one number or one curve "
    "and seeing that number is the point. Choose 'card' only if nothing "
    "else fits. Never draw a number the post does not already state."
)

DESCRIBE_WITHOUT_ILLUSTRATION = (
    "The picture that goes with this post. Every post gets one. Choose "
    "'line' when the post is about how one quantity changes with another. "
    "Choose 'bar' to compare a handful of named things. Choose 'stat' when "
    "the post turns on one striking number. Choose 'card' for everything "
    "else: a title card carrying the post's hook as a headline. Prefer a "
    "chart or a stat whenever the post states a number worth seeing. Never "
    "draw a number the post does not already state."
)

VISUAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": DESCRIBE_WITH_ILLUSTRATION,
    "properties": {
        "kind": {
            "type": "string",
            # Illustration first: the order is part of what the model reads.
            "enum": ["illustration", "line", "bar", "stat", "card"],
            "description": "Which to make.",
        },
        "title": {
            "type": "string",
            "description": (
                "Headline across the top; for 'stat' the short line above "
                "the number; for 'card' the headline itself, at most "
                f"{CARD_HEADLINE_LIMIT} characters, the post's hook rather "
                "than its subject. Empty for 'illustration'."
            ),
        },
        "subtitle": {
            "type": "string",
            "description": "One clause under the title. May be empty.",
        },
        "alt": {
            "type": "string",
            "description": (
                "Alt text describing the picture for someone who cannot see "
                "it, including the numbers it shows."
            ),
        },
        "x_label": {"type": "string", "description": "Horizontal axis label. Line and bar."},
        "y_label": {"type": "string", "description": "Vertical axis label. Line and bar."},
        "points": {
            "type": "array",
            "description": (
                "The curve, for 'line'. Compute at least 4 and ideally 25 or "
                "more evenly spaced points yourself from the relationship the "
                "post describes. Empty for every other kind."
            ),
            "items": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
        "linear_reference": {
            "type": "boolean",
            "description": (
                "For 'line': draw a faint straight line from the first point "
                "to the last, labelled 'what you'd expect'. Use it when the "
                "post is correcting an intuition that the relationship is "
                "proportional. False otherwise."
            ),
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"Bar labels, at most {MAX_BARS}. Empty for other kinds.",
        },
        "values": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Bar heights, one per category. Empty for other kinds.",
        },
        "highlight": {
            "type": "integer",
            "description": (
                "Index of the one point or bar the post is actually about, "
                "or -1 for none. Mark one at most."
            ),
        },
        "highlight_label": {
            "type": "string",
            "description": (
                "Two short lines beside the marked point, each a number and "
                "its unit. Empty if nothing is marked."
            ),
        },
        "figure": {
            "type": "string",
            "description": "For 'stat': the number itself, with its unit. Empty otherwise.",
        },
        "caption": {
            "type": "string",
            "description": (
                "For 'stat': one or two lines under the number doing the "
                "arithmetic. Empty otherwise."
            ),
        },
        "prompt": {
            "type": "string",
            "description": (
                "For 'illustration': what you picture, as one or two concrete "
                "sentences naming the objects, what they are doing and how "
                "they are arranged. It can be a scene, a single object, or a "
                "visual metaphor for the idea - make it yours, the thing that "
                "would make someone stop scrolling. The style is fixed for "
                "you, so no style words. No text in the image, no brand or "
                "real person's names, nothing photographic. Empty otherwise."
            ),
        },
    },
    "required": [
        "kind", "title", "subtitle", "alt", "x_label", "y_label", "points",
        "linear_reference", "categories", "values", "highlight",
        "highlight_label", "figure", "caption", "prompt",
    ],
    "additionalProperties": False,
}


def schema(can_illustrate: bool) -> dict[str, Any]:
    """The visual question, offering only what this run can actually draw.

    Without an image key an illustration can never be made, so offering it
    would let the model pick the one thing that is guaranteed to fall back to
    a plain title card, on exactly the posts where it judged a picture of the
    subject would serve best.
    """
    out = copy.deepcopy(VISUAL_SCHEMA)
    if not can_illustrate:
        out["properties"]["kind"]["enum"].remove("illustration")
        out["description"] = DESCRIBE_WITHOUT_ILLUSTRATION
    return out


@dataclass
class Visual:
    kind: str = "none"
    title: str = ""
    subtitle: str = ""
    alt: str = ""
    x_label: str = ""
    y_label: str = ""
    points: list[tuple[float, float]] = field(default_factory=list)
    linear_reference: bool = False
    categories: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    highlight: int = -1
    highlight_label: str = ""
    figure: str = ""
    caption: str = ""
    prompt: str = ""


@dataclass
class Rendered:
    path: Path
    alt: str
    kind: str


def _finite(value: Any) -> float | None:
    # OverflowError is neither of the other two: float(2**1024) raises it, and
    # a 309-digit integer is a perfectly legal answer under a "number" schema.
    # A post about key spaces or factorials would otherwise kill the run.
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # A model that computes a curve can hand back inf or nan at an asymptote.
    # matplotlib draws those as a silently broken line rather than raising.
    return number if math.isfinite(number) else None


def _items(value: Any) -> list:
    """Whatever arrived where an array was promised, as something iterable."""
    return value if isinstance(value, list) else []


def parse(data: Any) -> Visual:
    """Read the model's answer defensively. Anything odd becomes 'none'.

    The guard is here rather than at each conversion because this function
    promises total defensiveness and the list of ways a number can misbehave
    is not one anybody enumerates correctly in advance. render() takes the
    same approach for the same reason.
    """
    try:
        return _parse(data)
    except Exception as exc:  # noqa: BLE001 - a bad spec must not cost the post
        log.warning("could not read the visual spec (%s); posting as text", exc)
        return Visual()


def _parse(data: Any) -> Visual:
    if not isinstance(data, dict):
        return Visual()

    kind = str(data.get("kind") or "none").strip().lower()
    if kind not in KINDS:
        log.warning("unknown visual kind %r; treating as none", kind)
        return Visual()

    try:
        highlight = int(data.get("highlight", -1))
    except (TypeError, ValueError, OverflowError):
        highlight = -1

    # Keep each point's position in the model's own array while filtering, so
    # that re-sorting below can carry the highlight with it.
    kept: list[tuple[int, float, float]] = []
    for index, item in enumerate(_items(data.get("points"))):
        if not isinstance(item, dict):
            continue
        x, y = _finite(item.get("x")), _finite(item.get("y"))
        if x is not None and y is not None:
            kept.append((index, x, y))

    # A curve drawn out of order folds back on itself, so the points are
    # sorted. But the model chose its highlight against the order it wrote,
    # and a marker that moves to a different point is worse than no marker:
    # the callout would state one pair of numbers beside another.
    kept.sort(key=lambda row: (row[1], row[2]))
    points = [(x, y) for _, x, y in kept]
    if 0 <= highlight:
        moved = [position for position, row in enumerate(kept) if row[0] == highlight]
        highlight = moved[0] if moved else -1

    values = [
        v for v in (_finite(v) for v in _items(data.get("values"))) if v is not None
    ]
    categories = [str(c) for c in _items(data.get("categories")) if str(c).strip()]

    return Visual(
        kind=kind,
        title=str(data.get("title") or "").strip(),
        subtitle=str(data.get("subtitle") or "").strip(),
        alt=str(data.get("alt") or "").strip()[:ALT_LIMIT],
        x_label=str(data.get("x_label") or "").strip(),
        y_label=str(data.get("y_label") or "").strip(),
        points=points,
        linear_reference=bool(data.get("linear_reference")),
        categories=categories,
        values=values,
        highlight=highlight,
        highlight_label=str(data.get("highlight_label") or "").strip(),
        figure=str(data.get("figure") or "").strip(),
        caption=str(data.get("caption") or "").strip(),
        prompt=str(data.get("prompt") or "").strip(),
    )


def problem(visual: Visual) -> str | None:
    """Why this cannot be drawn, or None if it can.

    Mechanical checks only. Whether the picture is a good idea is the model's
    call; whether it is a drawable one is this function's.
    """
    if visual.kind == "none":
        return "no visual requested"

    if not visual.alt:
        return "no alt text"

    if visual.kind == "line":
        if len(visual.points) < MIN_POINTS:
            return f"only {len(visual.points)} usable points, need {MIN_POINTS}"
        if len(visual.points) > MAX_POINTS:
            return f"{len(visual.points)} points is more than {MAX_POINTS}"
        xs = {p[0] for p in visual.points}
        ys = {p[1] for p in visual.points}
        # A vertical or flat line is not a relationship, it is a mistake.
        if len(xs) < 2 or len(ys) < 2:
            return "the points do not vary in both directions"
        return None

    if visual.kind == "bar":
        if len(visual.categories) < 2:
            return "fewer than two bars"
        if len(visual.categories) > MAX_BARS:
            return f"{len(visual.categories)} bars is more than {MAX_BARS}"
        if len(visual.values) != len(visual.categories):
            return (
                f"{len(visual.categories)} categories against "
                f"{len(visual.values)} values"
            )
        if len(set(visual.values)) < 2:
            return "every bar is the same height"
        return None

    if visual.kind == "stat":
        if not visual.figure:
            return "no figure to show"
        return None

    if visual.kind == "card":
        if not visual.title:
            return "no headline for the card"
        return None

    if visual.kind == "illustration":
        if not visual.prompt:
            return "no prompt to draw from"
        return None

    return f"unknown kind {visual.kind!r}"


def render(
    visual: Visual,
    directory: Path,
    illustrator: Callable[[str, Path], bool] | None = None,
) -> Rendered | None:
    """Draw it, or return None and let the post go out as text."""
    reason = problem(visual)
    if reason:
        if visual.kind != "none":
            log.info("not drawing a %s: %s", visual.kind, reason)
        return None

    # Imported here, not at module scope, so that a broken or missing
    # matplotlib costs the picture rather than the post. This module is
    # reachable from main's import list; a drawing library is not a reason
    # for the whole account to stop.
    try:
        from . import chart
    except ImportError as exc:
        log.warning("cannot draw: %s", exc)
        return None

    path = directory / "post.png"

    try:
        # Inside the guard, not above it: a full disk, a read-only checkout or
        # a stray file where out/ should be is exactly the kind of thing that
        # must cost the picture and not the post.
        directory.mkdir(parents=True, exist_ok=True)

        if visual.kind == "line":
            chart.draw_line(
                path,
                title=visual.title,
                subtitle=visual.subtitle,
                x_label=visual.x_label,
                y_label=visual.y_label,
                points=visual.points,
                highlight=visual.highlight,
                highlight_label=visual.highlight_label,
                linear_reference=visual.linear_reference,
            )
        elif visual.kind == "bar":
            chart.draw_bar(
                path,
                title=visual.title,
                subtitle=visual.subtitle,
                x_label=visual.x_label,
                y_label=visual.y_label,
                categories=visual.categories,
                values=visual.values,
                highlight=visual.highlight,
            )
        elif visual.kind == "stat":
            chart.draw_stat(
                path,
                title=visual.title,
                figure_text=visual.figure,
                subtitle=visual.subtitle,
                caption=visual.caption,
            )
        elif visual.kind == "card":
            chart.draw_card(
                path,
                headline=visual.title[:CARD_HEADLINE_LIMIT],
                subtitle=visual.subtitle,
            )
        elif visual.kind == "illustration":
            if illustrator is None:
                log.info("an illustration was chosen but no image key is set")
                return None
            if not illustrator(visual.prompt, path):
                return None

        # Also inside: stat() on a path a failing filesystem just lost raises
        # as readily as the drawing did.
        if not path.exists() or path.stat().st_size == 0:
            log.warning("the %s rendered to nothing", visual.kind)
            return None
    except Exception as exc:  # noqa: BLE001 - a drawing bug must not lose the post
        log.warning("could not draw a %s: %s", visual.kind, exc)
        return None

    return Rendered(path=path, alt=visual.alt, kind=visual.kind)


def _hook(post: str) -> str:
    """The post's first sentence, cut at a word if it runs long."""
    text = " ".join(post.split())
    for end in (". ", "? ", "! ", ": "):
        cut = text.find(end)
        if 0 < cut < CARD_HEADLINE_LIMIT:
            return text[: cut + 1].rstrip(":")
    if len(text) <= CARD_HEADLINE_LIMIT:
        return text
    return text[: CARD_HEADLINE_LIMIT].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def fallback_card(post: str, wanted: Visual) -> Visual:
    """A title card from the post itself, for when nothing else was drawn.

    Every post gets a picture, so when the model's choice cannot be made (no
    image key, an unreadable spec, a chart that failed to render) this is
    what goes out instead. It uses the model's own headline when there is
    one, and the post's opening sentence when there is not, so it never
    claims anything the post does not.
    """
    headline = wanted.title if wanted.kind == "card" and wanted.title else _hook(post)
    return Visual(
        kind="card",
        title=headline,
        alt=f"A title card reading: {headline}",
    )
