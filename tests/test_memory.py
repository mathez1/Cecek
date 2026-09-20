import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.memory import (
    PostRecord,
    append_post,
    load_posts,
    load_state,
    month_key,
    posts_this_month,
    record_failure,
    record_skip,
    record_success,
    save_state,
    touch_run,
)

JAN = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
FEB = datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)


class TestPostLog:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "posts.jsonl"
        record = PostRecord(
            text="hello world",
            topic="greeting",
            rationale="because",
            sources=["https://example.com"],
            tweet_id="123",
        )
        append_post(path, record)

        loaded = load_posts(path)
        assert len(loaded) == 1
        assert loaded[0].text == "hello world"
        assert loaded[0].tweet_id == "123"
        assert loaded[0].sources == ["https://example.com"]

    def test_missing_file_is_empty(self, tmp_path):
        assert load_posts(tmp_path / "nope.jsonl") == []

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "posts.jsonl"
        path.write_text(
            '{"text": "good one"}\n'
            "this is not json at all\n"
            "\n"
            '{"text": "another good one"}\n',
            encoding="utf-8",
        )
        loaded = load_posts(path)
        assert [r.text for r in loaded] == ["good one", "another good one"]

    def test_unknown_fields_are_ignored(self, tmp_path):
        path = tmp_path / "posts.jsonl"
        path.write_text(
            json.dumps({"text": "hi", "from_a_future_version": 42}) + "\n",
            encoding="utf-8",
        )
        assert load_posts(path)[0].text == "hi"

    def test_limit_returns_the_newest(self, tmp_path):
        path = tmp_path / "posts.jsonl"
        for i in range(10):
            append_post(path, PostRecord(text=f"post {i}"))
        loaded = load_posts(path, limit=3)
        assert [r.text for r in loaded] == ["post 7", "post 8", "post 9"]


class TestState:
    def test_missing_file_gives_defaults(self, tmp_path):
        state = load_state(tmp_path / "state.json")
        assert state["total_posts"] == 0
        assert state["monthly"] == {}
        assert state["consecutive_failures"] == 0

    def test_corrupt_file_does_not_crash(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ not json", encoding="utf-8")
        assert load_state(path)["total_posts"] == 0

    def test_non_object_json_does_not_crash(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_state(path)["total_posts"] == 0

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        state = load_state(path)
        record_success(state, JAN)
        save_state(path, state)

        reloaded = load_state(path)
        assert reloaded["total_posts"] == 1
        assert posts_this_month(reloaded, JAN) == 1

    def test_monthly_counts_are_per_month(self):
        state = load_state(Path("/nonexistent"))
        record_success(state, JAN)
        record_success(state, JAN)
        record_success(state, FEB)

        assert posts_this_month(state, JAN) == 2
        assert posts_this_month(state, FEB) == 1
        assert state["total_posts"] == 3

    def test_success_clears_failure_streak(self):
        state = load_state(Path("/nonexistent"))
        record_failure(state, "boom")
        record_failure(state, "boom again")
        assert state["consecutive_failures"] == 2

        record_success(state, JAN)
        assert state["consecutive_failures"] == 0
        assert state["last_status"] == "posted"

    def test_skip_does_not_count_as_failure(self):
        state = load_state(Path("/nonexistent"))
        record_failure(state, "boom")
        record_skip(state, "budget reached")
        assert state["consecutive_failures"] == 0
        assert "budget reached" in state["last_status"]

    def test_monthly_history_is_pruned(self):
        state = load_state(Path("/nonexistent"))
        for year in range(2020, 2026):
            for month in range(1, 13):
                record_success(state, datetime(year, month, 1, tzinfo=timezone.utc))
        assert len(state["monthly"]) <= 24
        # the most recent month survives pruning
        assert month_key(datetime(2025, 12, 1, tzinfo=timezone.utc)) in state["monthly"]

    def test_touch_run_records_time(self):
        state = load_state(Path("/nonexistent"))
        touch_run(state, JAN)
        assert state["last_run_at"].startswith("2026-01-15")
