"""Tests for the response parsing in bot/brain.py.

The pipeline tests replace Brain wholesale, so this is the only place the
block-walking and the pause_turn resume loop are actually exercised. The fake
responses mirror the shapes the anthropic SDK really returns: a thinking block
first, several text blocks, and tool result blocks whose `content` is a list on
success and a single error object on failure.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import bot.brain as brain_mod
from bot.brain import Brain, BrainError, Exploration, _sources_of, _text_of
from bot.config import Config


def text(t):
    return NS(type="text", text=t)


def thinking(t="reasoning..."):
    return NS(type="thinking", thinking=t)


def search_ok(*urls):
    return NS(
        type="web_search_tool_result",
        content=[NS(type="web_search_result", url=u, title="t") for u in urls],
    )


def search_err(code="max_uses_exceeded"):
    return NS(type="web_search_tool_result", content=NS(error_code=code))


def fetch_ok(url):
    return NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url=url))


def fetch_err(code="url_not_accessible"):
    return NS(type="web_fetch_tool_result", content=NS(error_code=code))


def response(content, stop_reason="end_turn", stop_details=None):
    return NS(content=content, stop_reason=stop_reason, stop_details=stop_details)


@pytest.fixture
def cfg():
    return Config(
        anthropic_api_key="sk-test",
        x_api_key="k", x_api_secret="s",
        x_access_token="t", x_access_token_secret="ts",
        max_search_rounds=3,
    )


@pytest.fixture
def brain(cfg, monkeypatch):
    monkeypatch.setattr(brain_mod.anthropic, "Anthropic", lambda **kw: NS(
        beta=NS(messages=NS(create=None)), messages=NS(create=None)
    ))
    return Brain(cfg)


class TestTextExtraction:
    def test_joins_several_text_blocks_and_skips_thinking(self):
        r = response([thinking(), text("first"), search_ok("https://a"), text("second")])
        assert _text_of(r) == "first\nsecond"

    def test_ignores_blank_blocks(self):
        assert _text_of(response([text("  "), text("real"), text("")])) == "real"

    def test_no_text_blocks_gives_empty(self):
        assert _text_of(response([thinking(), search_ok("https://a")])) == ""


class TestSourceExtraction:
    def test_collects_search_and_fetch_urls(self):
        r = response([
            search_ok("https://a.com/1", "https://b.com/2"),
            fetch_ok("https://c.com/3"),
            text("notes"),
        ])
        assert _sources_of(r) == ["https://a.com/1", "https://b.com/2", "https://c.com/3"]

    def test_deduplicates_preserving_order(self):
        r = response([search_ok("https://a", "https://b"), search_ok("https://a", "https://c")])
        assert _sources_of(r) == ["https://a", "https://b", "https://c"]

    def test_a_search_error_does_not_crash(self):
        # On error the SDK gives a single object, not a list. Indexing it would
        # raise; the code has to branch on the type first.
        r = response([search_err(), text("carried on anyway")])
        assert _sources_of(r) == []

    def test_a_fetch_error_does_not_crash(self):
        assert _sources_of(response([fetch_err(), text("ok")])) == []

    def test_mixed_success_and_error(self):
        r = response([search_ok("https://a"), search_err(), fetch_ok("https://b")])
        assert _sources_of(r) == ["https://a", "https://b"]

    def test_no_tool_blocks_gives_nothing(self):
        assert _sources_of(response([text("just text")])) == []


class TestExplore:
    def _wire(self, brain, responses, monkeypatch):
        calls = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        monkeypatch.setattr(brain, "_create", fake_create)
        return calls

    def test_returns_notes_and_sources(self, brain, monkeypatch):
        self._wire(brain, [response([text("my notes"), search_ok("https://a")])], monkeypatch)
        result = brain.explore("digest", [], "persona")

        assert result.notes == "my notes"
        assert result.sources == ["https://a"]
        assert result.searched is True

    def test_empty_notes_is_an_error(self, brain, monkeypatch):
        self._wire(brain, [response([thinking()])], monkeypatch)
        with pytest.raises(BrainError, match="no text"):
            brain.explore("digest", [], "persona")

    def test_refusal_is_an_error(self, brain, monkeypatch):
        self._wire(brain, [response([], "refusal", NS(category="cyber"))], monkeypatch)
        with pytest.raises(BrainError, match="declined"):
            brain.explore("digest", [], "persona")

    def test_pause_turn_resumes_and_accumulates(self, brain, monkeypatch):
        paused = response([text("partial"), search_ok("https://a")], "pause_turn")
        done = response([text("complete"), search_ok("https://b")], "end_turn")
        calls = self._wire(brain, [paused, done], monkeypatch)

        result = brain.explore("digest", [], "persona")

        assert len(calls) == 2, "did not resume the paused turn"
        # The resume resends the original user turn plus the paused assistant
        # turn, with no extra user message.
        resumed = calls[1]["messages"]
        assert resumed[0]["role"] == "user"
        assert resumed[-1]["role"] == "assistant"
        assert result.notes == "complete"

    def test_pause_turn_is_bounded(self, brain, monkeypatch):
        always_paused = response([text("still going")], "pause_turn")
        calls = self._wire(brain, [always_paused], monkeypatch)

        result = brain.explore("digest", [], "persona")

        # 1 initial call + max_search_rounds resumes, then it gives up and uses
        # what it has rather than looping forever.
        assert len(calls) == 1 + brain.cfg.max_search_rounds
        assert result.notes == "still going"

    def test_web_search_off_sends_no_tools(self, cfg, monkeypatch):
        monkeypatch.setattr(brain_mod.anthropic, "Anthropic", lambda **kw: NS())
        b = Brain(Config(**{**cfg.__dict__, "enable_web_search": False}))
        calls = self._wire(b, [response([text("notes")])], monkeypatch)

        result = b.explore("digest", [], "persona")
        assert "tools" not in calls[0]
        assert result.searched is False

    def test_persona_goes_in_the_system_prompt(self, brain, monkeypatch):
        calls = self._wire(brain, [response([text("notes")])], monkeypatch)
        brain.explore("digest", [], "MY PERSONA")
        assert calls[0]["system"][0]["text"] == "MY PERSONA"

    def test_recent_posts_reach_the_prompt(self, brain, monkeypatch):
        calls = self._wire(brain, [response([text("notes")])], monkeypatch)
        brain.explore("digest", ["an earlier post"], "persona")
        assert "an earlier post" in calls[0]["messages"][0]["content"]


class TestCompose:
    def _wire(self, brain, payload, monkeypatch, stop_reason="end_turn"):
        calls = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            blocks = [thinking()]
            if payload is not None:
                blocks.append(text(payload))
            return response(blocks, stop_reason)

        monkeypatch.setattr(brain, "_create", fake_create)
        return calls

    def test_parses_the_json(self, brain, monkeypatch):
        self._wire(brain, json.dumps({
            "post": "a post", "topic": "t", "rationale": "r",
            "sources": ["https://a"], "confidence": "high",
        }), monkeypatch)

        draft = brain.compose(Exploration(notes="n"), [], "persona")
        assert draft.post == "a post"
        assert draft.confidence == "high"
        assert draft.sources == ["https://a"]

    def test_requests_the_schema_and_no_tools(self, brain, monkeypatch):
        calls = self._wire(brain, json.dumps({"post": "p"}), monkeypatch)
        brain.compose(Exploration(notes="n"), [], "persona")

        assert calls[0]["output_config"]["format"]["type"] == "json_schema"
        assert "tools" not in calls[0], "composition must not run server tools"

    def test_bad_json_is_an_error(self, brain, monkeypatch):
        self._wire(brain, "{not json", monkeypatch)
        with pytest.raises(BrainError, match="valid JSON"):
            brain.compose(Exploration(notes="n"), [], "persona")

    def test_empty_post_is_an_error(self, brain, monkeypatch):
        self._wire(brain, json.dumps({"post": "   "}), monkeypatch)
        with pytest.raises(BrainError, match="empty post"):
            brain.compose(Exploration(notes="n"), [], "persona")

    def test_missing_optional_fields_default_sanely(self, brain, monkeypatch):
        self._wire(brain, json.dumps({"post": "a post"}), monkeypatch)
        draft = brain.compose(Exploration(notes="n"), [], "persona")

        assert draft.topic == ""
        assert draft.sources == []
        assert draft.confidence == "medium"

    def test_a_non_list_sources_field_is_tolerated(self, brain, monkeypatch):
        self._wire(brain, json.dumps({"post": "a post", "sources": "not a list"}), monkeypatch)
        assert brain.compose(Exploration(notes="n"), [], "persona").sources == []

    def test_feedback_is_passed_through(self, brain, monkeypatch):
        calls = self._wire(brain, json.dumps({"post": "p"}), monkeypatch)
        brain.compose(Exploration(notes="n"), [], "persona", feedback="TOO LONG")
        assert "TOO LONG" in calls[0]["messages"][0]["content"]
