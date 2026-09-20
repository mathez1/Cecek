import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bot.config import POST_CHAR_LIMIT, Config
from bot.guard import (
    TCO_URL_WEIGHT,
    check_draft,
    contains_link,
    normalize_for_compare,
    similarity,
    weighted_length,
)

BANNED = Config.__dataclass_fields__["banned_openers"].default_factory()


def guard(text, recent=()):
    return check_draft(
        text,
        list(recent),
        char_limit=POST_CHAR_LIMIT,
        banned_openers=BANNED,
        similarity_threshold=0.72,
    )


class TestWeightedLength:
    def test_ascii_counts_one_each(self):
        assert weighted_length("hello") == 5

    def test_cjk_counts_double(self):
        assert weighted_length("日本語") == 6

    def test_emoji_counts_double(self):
        assert weighted_length("🙂") == 2

    def test_url_counts_as_fixed_tco_weight(self):
        # "see " is 4, the URL collapses to the t.co weight regardless of length
        text = "see https://example.com/a/very/long/path/that/goes/on/forever?x=1"
        assert weighted_length(text) == 4 + TCO_URL_WEIGHT

    def test_two_urls_both_collapse(self):
        assert weighted_length("https://a.com https://b.com") == TCO_URL_WEIGHT * 2 + 1

    def test_combining_marks_do_not_add(self):
        # e + combining acute should weigh the same as a plain e
        assert weighted_length("é") == 1


class TestSimilarity:
    def test_identical_is_one(self):
        assert similarity("the same text", "the same text") == 1.0

    def test_punctuation_and_case_ignored(self):
        assert similarity("Hello, World!", "hello world") == 1.0

    def test_unrelated_is_low(self):
        score = similarity(
            "clipper ships beat steamers on the tea route until 1869",
            "the price of eggs fell by a third last quarter",
        )
        assert score < 0.4

    def test_reordered_restatement_is_caught(self):
        a = "Norway sells more electric cars than petrol cars every single month now"
        b = "Every month now, Norway sells more electric cars than petrol cars"
        assert similarity(a, b) > 0.72

    def test_empty_is_zero(self):
        assert similarity("", "anything") == 0.0

    def test_normalize_strips_urls(self):
        assert "http" not in normalize_for_compare("look https://example.com here")

    def test_tighter_rewrite_of_an_earlier_post_is_caught(self):
        # Regression: jaccard alone scored this 0.714, just under the 0.72
        # threshold, because the longer original has words the rewrite dropped.
        # Containment scores it 0.909, which is the honest answer.
        original = (
            "Portland cement does not dry, it reacts. It will cure perfectly "
            "well underwater, which is how Roman harbour concrete set at all."
        )
        rewrite = (
            "Cement reacts, it does not dry, so Roman harbour concrete could "
            "cure underwater perfectly well."
        )
        assert similarity(original, rewrite) >= 0.72

    def test_same_subject_different_fact_is_not_caught(self):
        # The other side of that fix: containment must not flag two genuinely
        # different things that happen to share a subject.
        a = (
            "The Antikythera mechanism has 30 surviving bronze gears, a level "
            "of gearing that does not reappear for 1400 years."
        )
        b = (
            "The Antikythera mechanism was pulled from a shipwreck in 1901 by "
            "sponge divers working off Crete."
        )
        assert similarity(a, b) < 0.72

    def test_short_posts_do_not_trip_containment(self):
        # Too few long tokens for an overlap to mean anything.
        assert similarity("Honey never spoils.", "Glass is not a liquid.") < 0.72


class TestCheckDraft:
    def test_a_good_post_passes(self):
        result = guard(
            "The Antikythera mechanism has 30 surviving bronze gears. "
            "Nothing of comparable gearing shows up again in the "
            "archaeological record for roughly 1400 years."
        )
        assert result.ok, result.problems

    def test_too_long_is_rejected(self):
        result = guard("x" * 300)
        assert not result.ok
        assert any("too long" in p for p in result.problems)

    def test_empty_is_rejected(self):
        assert not guard("   ").ok

    def test_too_short_is_rejected(self):
        result = guard("neat")
        assert not result.ok
        assert any("too short" in p for p in result.problems)

    @pytest.mark.parametrize(
        "opener",
        [
            "Ever wondered why bridges hum in the wind? They do.",
            "Most people don't know that honey never spoils, but it does not.",
            "Here's the thing: the metric system arrived later than you think.",
            "Hot take: the QWERTY layout was never designed to slow typists.",
        ],
    )
    def test_banned_openers_are_rejected(self, opener):
        result = guard(opener)
        assert not result.ok
        assert any("cliche" in p for p in result.problems)

    def test_quoted_banned_opener_still_caught(self):
        result = guard('"Let that sink in" is what people say about nothing at all.')
        assert not result.ok

    def test_hashtags_are_rejected(self):
        result = guard(
            "Steel production peaked in 2020 and has fallen since. #steel #economics"
        )
        assert not result.ok
        assert any("hashtag" in p for p in result.problems)

    def test_em_dash_is_rejected(self):
        result = guard(
            "The first webcam watched a coffee pot — the pot outlived the lab itself."
        )
        assert not result.ok
        assert any("dash" in p for p in result.problems)

    @pytest.mark.parametrize(
        "bait",
        [
            "Interest rates move slower than people expect. Thoughts?",
            "Cities were denser in 1900 than today. Agree?",
            "The lightbulb was not invented by Edison. Change my mind.",
            "RT if you have ever read a whole terms of service document.",
        ],
    )
    def test_engagement_bait_is_rejected(self, bait):
        assert not guard(bait).ok

    def test_thread_marker_is_rejected(self):
        result = guard("1/7 A short history of the shipping container and its effects.")
        assert not result.ok

    def test_near_duplicate_of_history_is_rejected(self):
        previous = "Norway now sells more electric cars than petrol cars every month."
        result = guard(
            "Every month, Norway sells more electric cars than petrol ones now.",
            recent=[previous],
        )
        assert not result.ok
        assert any("similar" in p for p in result.problems)
        assert result.closest_match == previous

    def test_novel_post_against_history_passes(self):
        result = guard(
            "Portland cement sets by chemical reaction, not by drying. "
            "It will cure perfectly well underwater, which is how the "
            "Romans built harbours.",
            recent=["Norway now sells more electric cars than petrol cars."],
        )
        assert result.ok, result.problems
        assert result.closest_match is None

    def test_feedback_mentions_every_problem(self):
        result = guard("x" * 300 + " #tag")
        text = result.feedback()
        assert "too long" in text
        assert "hashtag" in text
