"""Checks a draft has to survive before it is allowed near the X API.

The model is given real freedom over *what* to say, so these guards deliberately
police only mechanical failures: too long, already said, opens with a cliche,
looks like a thread, smells like engagement bait. They never judge the idea.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# From twitter/twitter-text config v3 (the file X's own clients validate
# against). Weights are scaled by 100 there; normalised to 1 and 2 here.
#
# The important and counter-intuitive part: the DEFAULT weight is 2. Only the
# ranges below cost 1. Latin Extended Additional, most symbols, arrows, CJK and
# every emoji all cost 2. Validating with len() will happily build a post that
# X rejects with a 400.
TCO_URL_WEIGHT = 23  # transformedURLLength: every link costs exactly this
DEFAULT_WEIGHT = 2

_SINGLE_WEIGHT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x10FF),
    (0x2000, 0x200D),
    (0x2010, 0x201F),
    (0x2032, 0x2037),
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# One emoji costs 2 whether it is a single code point or a seven-code-point ZWJ
# family sequence, because X parses emoji before weighting. This matches whole
# sequences (base + modifiers + ZWJ-joined parts + flag tags + keycaps). If a
# sequence slips past it the parts are counted separately, which over-counts:
# the safe direction to be wrong in.
_EMOJI_CORE = (
    r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    r"\U00002190-\U000021FF\U0001F1E6-\U0001F1FF\U00002B50\U00002934-\U00002935"
    r"\U000025A0-\U000025FF\U00002049\U0000203C\U00002122\U00002139]"
)
_EMOJI_MODS = r"[\U0001F3FB-\U0001F3FF\uFE0F\uFE0E\u20E3\U000E0020-\U000E007F]"
_EMOJI_RE = re.compile(
    rf"(?:[0-9#*]\uFE0F?\u20E3)"                     # keycap sequences
    rf"|(?:{_EMOJI_CORE}{_EMOJI_MODS}*(?:\u200D{_EMOJI_CORE}{_EMOJI_MODS}*)*)"
)


def weighted_length(text: str) -> int:
    """X's weighted character count for a standard post. Budget is 280."""
    # 1. Links collapse to a fixed weight whatever their real length.
    without_urls, url_count = _URL_RE.subn("", text)
    total = url_count * TCO_URL_WEIGHT

    normalized = unicodedata.normalize("NFC", without_urls)

    # 2. Each emoji sequence is one unit at the default weight.
    without_emoji, emoji_count = _EMOJI_RE.subn("", normalized)
    total += emoji_count * DEFAULT_WEIGHT

    # 3. Everything left is weighted by code point range.
    for char in without_emoji:
        if unicodedata.combining(char):
            continue  # rides along with its base character
        total += 1 if _is_single_weight(ord(char)) else DEFAULT_WEIGHT

    return total


def _is_single_weight(code: int) -> bool:
    return any(low <= code <= high for low, high in _SINGLE_WEIGHT_RANGES)


def normalize_for_compare(text: str) -> str:
    text = _URL_RE.sub(" ", text.lower())
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


_BAIT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bagree\?\s*$", "ends with engagement bait ('agree?')"),
    (r"\bthoughts\?\s*$", "ends with engagement bait ('thoughts?')"),
    (r"\bam i wrong\?\s*$", "ends with engagement bait"),
    (r"\bchange my mind\b", "engagement bait ('change my mind')"),
    (r"\brt if\b", "engagement bait ('RT if')"),
    (r"\blike (?:and|&) (?:retweet|share)\b", "engagement bait"),
    (r"\bfollow (?:me|for more)\b", "self-promotion / follow bait"),
    (r"^\s*\d+\s*[/|]\s*\d+\b", "looks like a numbered thread part"),
    (r"\b(?:1/|1/\d+)\s*$", "looks like the start of a thread"),
    (r"#\w+", "contains a hashtag"),
)

# Containment is only trustworthy once there is enough vocabulary for an
# overlap to mean something. Below this, two unrelated short posts can share
# most of their long words by accident.
_MIN_TOKENS_FOR_CONTAINMENT = 5


def similarity(a: str, b: str) -> float:
    """0.0 to 1.0, where 1.0 means "you already said this".

    Three views, because each misses a shape of repetition the others catch:

      ratio        character overlap. Catches near-identical phrasing.
      jaccard      shared vocabulary. Catches reordering and word swaps.
      containment  how much of the SHORTER post is inside the longer one.
                   Catches the common failure: a tighter rewrite of an
                   earlier post, where jaccard is dragged down by the words
                   the longer original has and the rewrite dropped.

    The highest wins. A false positive costs one wasted recompose; a false
    negative puts the same thought on the timeline twice.
    """
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    ratio = SequenceMatcher(None, na, nb).ratio()

    tokens_a = {t for t in na.split() if len(t) > 3}
    tokens_b = {t for t in nb.split() if len(t) > 3}
    if not tokens_a or not tokens_b:
        return ratio

    shared = len(tokens_a & tokens_b)
    jaccard = shared / len(tokens_a | tokens_b)

    smaller = min(len(tokens_a), len(tokens_b))
    containment = (
        shared / smaller if smaller >= _MIN_TOKENS_FOR_CONTAINMENT else 0.0
    )

    return max(ratio, jaccard, containment)


@dataclass
class GuardResult:
    ok: bool
    problems: list[str]
    weighted_length: int
    closest_match: str | None = None
    closest_score: float = 0.0

    def feedback(self) -> str:
        lines = ["The draft was rejected for these reasons:"]
        lines += [f"- {p}" for p in self.problems]
        if self.closest_match:
            lines.append(
                f"\nClosest earlier post ({self.closest_score:.0%} similar):\n"
                f"  {self.closest_match}"
            )
        lines.append("\nWrite a different post that avoids all of the above.")
        return "\n".join(lines)


def check_draft(
    text: str,
    recent_posts: list[str],
    *,
    char_limit: int,
    banned_openers: tuple[str, ...],
    similarity_threshold: float,
    allow_links: bool = False,
) -> GuardResult:
    problems: list[str] = []
    stripped = text.strip()

    length = weighted_length(stripped)

    if not stripped:
        problems.append("the draft is empty")
    if length > char_limit:
        problems.append(
            f"too long: {length} weighted characters, the limit is {char_limit}"
        )
    if stripped and length < 25:
        problems.append(f"too short to be worth posting ({length} characters)")

    if not allow_links and _URL_RE.search(stripped):
        problems.append(
            "contains a link. Links are disabled: X bills a post containing a "
            "URL at roughly 13x the price of a plain one. Say the thing itself "
            "instead of pointing at it."
        )

    if stripped.count("\n\n") >= 3:
        problems.append("reads like a thread; it must be one standalone post")

    if "—" in stripped or "–" in stripped:
        problems.append("contains an em dash or en dash; the persona forbids them")

    lowered = stripped.lower().lstrip("\"'“‘ ")
    for opener in banned_openers:
        if lowered.startswith(opener):
            problems.append(f"opens with a banned cliche: {opener!r}")
            break

    for pattern, label in _BAIT_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE | re.MULTILINE):
            problems.append(label)

    closest_match: str | None = None
    closest_score = 0.0
    for previous in recent_posts:
        score = similarity(stripped, previous)
        if score > closest_score:
            closest_score, closest_match = score, previous

    if closest_score >= similarity_threshold:
        problems.append(
            f"too similar to an earlier post ({closest_score:.0%} overlap); "
            "say something genuinely new"
        )
    else:
        closest_match = None

    return GuardResult(
        ok=not problems,
        problems=problems,
        weighted_length=length,
        closest_match=closest_match,
        closest_score=closest_score,
    )
