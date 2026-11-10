"""The reporter / off-the-clock split.

The owner asked for about seven reports in ten. A model told a ratio picks
the same thing every run, because every run starts fresh, so the choice is
made in code from recent history. These pin the mix and the edges down.
"""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bot import guard as guard_module
from bot import modes
from bot.brain import Exploration, _compose_prompt, _explore_prompt
from bot.memory import PostRecord


def run(share, posts=200):
    history = []
    for _ in range(posts):
        history.append(NS(mode=modes.pick(history, share)))
    return [p.mode for p in history]


class TestTheMix:
    @pytest.mark.parametrize("share", [0.7, 0.5, 0.3, 0.8])
    def test_it_lands_on_the_target(self, share):
        picked = run(share)
        assert picked.count(modes.NEWS) / len(picked) == pytest.approx(share, abs=0.02)

    def test_it_holds_over_any_ten_posts_not_just_on_average(self):
        # A day of nine reports and a day of five would average out but read
        # as two different accounts.
        picked = run(0.7)[20:]
        for start in range(len(picked) - 10):
            window = picked[start:start + 10]
            assert 6 <= window.count(modes.NEWS) <= 8

    def test_a_fresh_account_starts_by_reporting(self):
        assert modes.pick([], 0.7) == modes.NEWS

    def test_history_from_before_modes_existed_counts_as_fresh(self):
        old = [NS(mode="") for _ in range(30)]
        assert modes.pick(old, 0.7) == modes.NEWS

    @pytest.mark.parametrize("share,expected", [(0, modes.FUN), (1, modes.NEWS), (-3, modes.FUN), (9, modes.NEWS)])
    def test_the_extremes(self, share, expected):
        assert set(run(share, 30)) == {expected}


class TestThePrompts:
    def test_reporting_asks_for_todays_news(self):
        prompt = _explore_prompt("digest", [], True, modes.NEWS)
        assert "REPORTING" in prompt
        assert "today" in prompt

    def test_off_the_clock_has_no_news_duty(self):
        prompt = _explore_prompt("digest", [], True, modes.FUN)
        assert "OFF THE CLOCK" in prompt
        assert "REPORTING" not in prompt

    def test_a_report_is_written_in_bullets(self):
        prompt = _compose_prompt(Exploration(notes="n", mode=modes.NEWS), [], None)
        assert '"• "' in prompt

    def test_the_mode_survives_a_retry(self):
        # The rewrite after a rejected draft is a fresh compose call. If the
        # mode lived anywhere but on the exploration, it would fall back.
        retry = _compose_prompt(Exploration(notes="n", mode=modes.NEWS), [], "too long")
        assert '"• "' in retry

    def test_off_the_clock_keeps_the_free_shape(self):
        prompt = _compose_prompt(Exploration(notes="n", mode=modes.FUN), [], None)
        assert '"• "' not in prompt


class TestTheModeIsRemembered:
    def test_it_round_trips(self):
        record = PostRecord.from_dict({"text": "x", "mode": "news"})
        assert record.mode == "news"

    def test_old_records_still_load(self):
        assert PostRecord.from_dict({"text": "x"}).mode == ""


class TestHashtags:
    def _check(self, text):
        return guard_module.check_draft(
            text, [], char_limit=280, banned_openers=(),
            similarity_threshold=0.72, allow_links=False,
        )

    def test_one_is_allowed(self):
        assert not any("hashtag" in p for p in self._check("Route du Rhum starts today #RouteDuRhum").problems)

    def test_two_are_not(self):
        assert any("hashtag" in p for p in self._check("Launch day #Space #NASA").problems)

    @pytest.mark.parametrize("text", ["Ranked #1 for a decade", "Written in C# and F#"])
    def test_things_that_are_not_hashtags(self, text):
        assert not any("hashtag" in p for p in self._check(text + " #OneReal").problems)

    def test_a_report_with_bullets_passes(self):
        report = (
            "Starship flew its tenth test today.\n"
            "• The booster was caught by the tower arms again\n"
            "• Each Raptor makes about 230 tonnes of thrust"
        )
        assert not self._check(report).problems


class TestPolitics:
    def test_it_is_on_the_reporting_beat(self):
        assert "politics" in _explore_prompt("d", [], True, modes.NEWS)

    def test_the_rules_travel_with_it(self):
        assert "never whose side" in _explore_prompt("d", [], True, modes.NEWS)

    def test_the_persona_has_the_rules(self):
        persona = (Path(__file__).resolve().parent.parent / "prompts/persona.md").read_text()
        section = persona.split("## Reporting politics")[1].split("##")[0]
        for rule in ("Never take a side", "Attribute claims", "never report a\n  result before it is official", "symbols only"):
            assert rule in section, rule

    def test_political_pictures_are_symbols_not_people(self):
        from bot import visual
        prompt_rule = visual.VISUAL_SCHEMA["properties"]["prompt"]["description"]
        assert "never" in prompt_rule and "a person" in prompt_rule
