"""End-to-end runs of bot.main with both network calls mocked out.

This is the test that matters: it exercises the real orchestration, guards,
memory writes and exit codes, and only fakes the two things that cost money.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import bot.config as config
import bot.explore as explore
import bot.main as main
from bot.brain import BrainError, Draft, Exploration
from bot.memory import load_posts, load_state
from bot.x_client import (
    PostResult,
    XAuthError,
    XDuplicateError,
    XQuotaError,
    XRateLimitError,
)

GOOD_POST = (
    "Portland cement does not dry, it reacts. It will cure perfectly well "
    "underwater, which is how Roman harbour concrete set at all."
)
OTHER_POST = (
    "The Antikythera mechanism has 30 surviving bronze gears. Nothing with "
    "comparable gearing appears again for about 1400 years."
)


class FakeBrain:
    """Stands in for Claude. Returns queued drafts, records the feedback it got."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.drafts = list(getattr(FakeBrain, "queue", [Draft(post=GOOD_POST)]))
        self.feedback_seen = []
        self.explore_calls = 0

    def explore(self, digest, recent, persona):
        self.explore_calls += 1
        return Exploration(notes="some notes", sources=["https://example.com/a"])

    def compose(self, exploration, recent, persona, feedback=None):
        self.feedback_seen.append(feedback)
        if not self.drafts:
            raise AssertionError("compose called more times than the test queued")
        item = self.drafts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeXClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.posted = []
        self.raises = list(getattr(FakeXClient, "queue", []))

    def post(self, text):
        if self.raises:
            exc = self.raises.pop(0)
            if exc is not None:
                raise exc
        self.posted.append(text)
        return PostResult(tweet_id="1234567890", text=text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point every path at a temp dir and stub out the network."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    (prompts_dir / "persona.md").write_text("Be interesting.", encoding="utf-8")
    (prompts_dir / "feeds.txt").write_text(
        "Example | https://example.com/feed\n", encoding="utf-8"
    )

    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(config, "POSTS_PATH", memory_dir / "posts.jsonl")
    monkeypatch.setattr(config, "STATE_PATH", memory_dir / "state.json")
    monkeypatch.setattr(config, "FEED_CACHE_PATH", memory_dir / "feed_cache.json")
    monkeypatch.setattr(config, "PERSONA_PATH", prompts_dir / "persona.md")
    monkeypatch.setattr(config, "FEEDS_PATH", prompts_dir / "feeds.txt")

    # No feed fetching in tests.
    monkeypatch.setattr(explore, "gather", lambda *a, **k: [])

    for key, value in [
        ("ANTHROPIC_API_KEY", "sk-ant-test"),
        ("X_API_KEY", "k"),
        ("X_API_SECRET", "s"),
        ("X_ACCESS_TOKEN", "t"),
        ("X_ACCESS_TOKEN_SECRET", "ts"),
    ]:
        monkeypatch.setenv(key, value)
    for key in [
        "DRY_RUN", "MODEL", "EFFORT", "ENABLE_WEB_SEARCH", "ALLOW_LINKS",
        "MONTHLY_POST_BUDGET", "FEED_SAMPLE_SIZE", "RECENT_POSTS_IN_CONTEXT",
    ]:
        monkeypatch.delenv(key, raising=False)

    FakeBrain.queue = [Draft(post=GOOD_POST, topic="concrete", rationale="it is neat")]
    FakeXClient.queue = []
    monkeypatch.setattr(main, "Brain", FakeBrain)
    monkeypatch.setattr(main, "XClient", FakeXClient)

    return SimpleRepo(memory_dir, prompts_dir)


class SimpleRepo:
    def __init__(self, memory_dir, prompts_dir):
        self.memory = memory_dir
        self.prompts = prompts_dir

    @property
    def posts(self):
        return load_posts(self.memory / "posts.jsonl")

    @property
    def state(self):
        return load_state(self.memory / "state.json")


class TestHappyPath:
    def test_posts_and_records_everything(self, repo):
        assert main.run() == main.EXIT_OK

        assert [p.text for p in repo.posts] == [GOOD_POST]
        assert repo.posts[0].tweet_id == "1234567890"
        assert repo.posts[0].topic == "concrete"
        assert repo.posts[0].url.endswith("/1234567890")

        state = repo.state
        assert state["total_posts"] == 1
        assert state["last_status"] == "posted"
        assert state["consecutive_failures"] == 0
        assert state["last_run_at"] is not None

    def test_second_run_sees_the_first_post(self, repo, monkeypatch):
        assert main.run() == main.EXIT_OK

        seen = {}
        original = FakeBrain.explore

        def spy(self, digest, recent, persona):
            seen["recent"] = list(recent)
            return original(self, digest, recent, persona)

        monkeypatch.setattr(FakeBrain, "explore", spy)
        FakeBrain.queue = [Draft(post=OTHER_POST)]

        assert main.run() == main.EXIT_OK
        assert seen["recent"] == [GOOD_POST]
        assert len(repo.posts) == 2


class TestDryRun:
    def test_writes_nothing_to_x(self, repo, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        assert main.run() == main.EXIT_OK

        assert repo.posts == []
        assert repo.state["last_status"] == "skipped: dry run"

    def test_runs_without_x_credentials(self, repo, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        for key in ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]:
            monkeypatch.delenv(key)
        assert main.run() == main.EXIT_OK


class TestGuardsInTheLoop:
    def test_a_rejected_draft_is_recomposed_with_feedback(self, repo):
        FakeBrain.queue = [
            Draft(post="x" * 400),          # too long
            Draft(post=GOOD_POST),          # acceptable
        ]
        assert main.run() == main.EXIT_OK

        assert [p.text for p in repo.posts] == [GOOD_POST]

    def test_low_confidence_drafts_are_not_posted(self, repo):
        FakeBrain.queue = [
            Draft(post=OTHER_POST, confidence="low"),
            Draft(post=GOOD_POST, confidence="high"),
        ]
        assert main.run() == main.EXIT_OK
        assert [p.text for p in repo.posts] == [GOOD_POST]

    def test_gives_up_after_max_attempts(self, repo):
        FakeBrain.queue = [Draft(post="#spam " + "x" * 400)] * 3
        assert main.run() == main.EXIT_NEEDS_HUMAN

        assert repo.posts == []
        assert repo.state["consecutive_failures"] == 1
        assert repo.state["last_status"].startswith("failed")

    def test_a_repeat_of_history_is_rejected(self, repo):
        assert main.run() == main.EXIT_OK

        # Same idea, reworded. Should be caught, then replaced.
        FakeBrain.queue = [
            Draft(post="Cement reacts, it does not dry, so Roman harbour "
                       "concrete could cure underwater perfectly well."),
            Draft(post=OTHER_POST),
        ]
        assert main.run() == main.EXIT_OK
        assert [p.text for p in repo.posts] == [GOOD_POST, OTHER_POST]

    def test_links_are_rejected_by_default(self, repo):
        FakeBrain.queue = [
            Draft(post="Concrete cures underwater, which is a real thing https://a.co/x"),
            Draft(post=GOOD_POST),
        ]
        assert main.run() == main.EXIT_OK
        assert [p.text for p in repo.posts] == [GOOD_POST]


class TestBudget:
    def test_stops_at_the_monthly_cap(self, repo, monkeypatch):
        monkeypatch.setenv("MONTHLY_POST_BUDGET", "1")
        assert main.run() == main.EXIT_OK
        assert len(repo.posts) == 1

        FakeBrain.queue = [Draft(post=OTHER_POST)]
        assert main.run() == main.EXIT_OK
        assert len(repo.posts) == 1, "posted past the budget"
        assert "budget reached" in repo.state["last_status"]

    def test_zero_means_unlimited(self, repo, monkeypatch):
        monkeypatch.setenv("MONTHLY_POST_BUDGET", "0")
        assert main.run() == main.EXIT_OK
        FakeBrain.queue = [Draft(post=OTHER_POST)]
        assert main.run() == main.EXIT_OK
        assert len(repo.posts) == 2


class TestXFailures:
    def test_rate_limit_is_a_soft_skip(self, repo):
        FakeXClient.queue = [XRateLimitError("slow down")]
        assert main.run() == main.EXIT_OK, "a self-healing limit should not go red"

        assert repo.posts == []
        assert repo.state["last_status"] == "skipped: rate limited by X"
        assert repo.state["consecutive_failures"] == 0

    def test_quota_exhaustion_needs_a_human(self, repo):
        FakeXClient.queue = [XQuotaError("usage cap exceeded")]
        assert main.run() == main.EXIT_NEEDS_HUMAN
        assert repo.state["consecutive_failures"] == 1

    def test_bad_credentials_need_a_human(self, repo):
        FakeXClient.queue = [XAuthError("401")]
        assert main.run() == main.EXIT_NEEDS_HUMAN
        assert "credentials" in repo.state["last_status"]

    def test_duplicate_triggers_one_rewrite_that_succeeds(self, repo):
        FakeXClient.queue = [XDuplicateError("duplicate content"), None]
        FakeBrain.queue = [Draft(post=GOOD_POST), Draft(post=OTHER_POST)]

        assert main.run() == main.EXIT_OK
        assert [p.text for p in repo.posts] == [OTHER_POST]

    def test_duplicate_twice_gives_up(self, repo):
        FakeXClient.queue = [XDuplicateError("dup"), XDuplicateError("dup again")]
        FakeBrain.queue = [Draft(post=GOOD_POST), Draft(post=OTHER_POST)]

        assert main.run() == main.EXIT_NEEDS_HUMAN
        assert repo.posts == []


class TestConfigFailures:
    def test_missing_anthropic_key_is_a_clean_failure(self, repo, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        assert main.run() == main.EXIT_NEEDS_HUMAN

    def test_missing_x_credentials_fail_when_not_dry_running(self, repo, monkeypatch):
        monkeypatch.delenv("X_ACCESS_TOKEN")
        assert main.run() == main.EXIT_NEEDS_HUMAN

    def test_invalid_effort_is_rejected(self, repo, monkeypatch):
        monkeypatch.setenv("EFFORT", "maximum")
        assert main.run() == main.EXIT_NEEDS_HUMAN


class TestBrainFailure:
    def test_api_failure_is_recorded_and_goes_red(self, repo):
        FakeBrain.queue = [BrainError("the API is down")]
        assert main.run() == main.EXIT_NEEDS_HUMAN
        assert repo.state["consecutive_failures"] == 1


class TestStepSummary:
    def test_summary_is_written_when_actions_provides_one(self, repo, tmp_path, monkeypatch):
        path = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))

        assert main.run() == main.EXIT_OK
        text = path.read_text(encoding="utf-8")
        assert "## Posted" in text
        assert GOOD_POST in text
        assert "1234567890" in text

    def test_missing_summary_path_is_harmless(self, repo, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        assert main.run() == main.EXIT_OK
