"""The view tracker.

Two things here are worth more than the rest. The first is which endpoint
gets called: reading your own timeline is an "Owned Read" at $0.001 a post
and reading the same posts by id is $0.005, so the wrong one is a five-fold
bill for identical numbers. The second is that none of this may ever fail a
run -- the posts are already published and the numbers are an observation
about them.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import tweepy

from bot import metrics, track
from bot.memory import PostRecord

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


class Cfg:
    x_api_key = "k"
    x_api_secret = "s"
    x_access_token = "t"
    x_access_token_secret = "ts"
    x_user_id = ""
    metrics_window_days = 7
    has_x_credentials = True


def public(impressions, **extra):
    base = {
        "impression_count": impressions,
        "like_count": 0,
        "reply_count": 0,
        "retweet_count": 0,
        "quote_count": 0,
        "bookmark_count": 0,
    }
    base.update(extra)
    return base


def tweet(tweet_id, impressions, age_days=0, **extra):
    return NS(
        id=tweet_id,
        public_metrics=public(impressions, **extra),
        created_at=NOW - timedelta(days=age_days),
    )


def page(tweets, next_token=None):
    return NS(data=tweets, meta={"next_token": next_token} if next_token else {})


class FakeClient:
    """Records which endpoint was asked, and with what."""

    pages: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.timeline_calls = []
        FakeClient.last = self

    def get_users_tweets(self, **kwargs):
        self.timeline_calls.append(kwargs)
        index = len(self.timeline_calls) - 1
        return FakeClient.pages[min(index, len(FakeClient.pages) - 1)]

    def get_tweets(self, **kwargs):
        raise AssertionError(
            "get_tweets costs $0.005 a post; the timeline costs $0.001"
        )

    def get_me(self, **kwargs):
        return NS(data=NS(id=12345))


@pytest.fixture
def fake(monkeypatch):
    FakeClient.pages = [page([tweet("1", 100)])]
    monkeypatch.setattr(metrics.tweepy, "Client", FakeClient)
    monkeypatch.setattr(track.tweepy, "Client", FakeClient)
    return FakeClient


class TestReadingTheNumbers:
    def test_the_view_count(self):
        assert metrics.read_metrics(public(412)).impressions == 412

    def test_both_spellings_of_repost(self):
        # The spec renamed retweet_count to repost_count; the docs did not.
        assert metrics.read_metrics({"retweet_count": 7}).reposts == 7
        assert metrics.read_metrics({"repost_count": 9}).reposts == 9

    def test_the_new_spelling_wins_when_both_appear(self):
        raw = {"repost_count": 9, "retweet_count": 7}
        assert metrics.read_metrics(raw).reposts == 9

    @pytest.mark.parametrize("raw", [None, "nope", [], {"impression_count": "lots"}])
    def test_nonsense_reads_as_zero_rather_than_raising(self, raw):
        assert metrics.read_metrics(raw).impressions == 0

    def test_engagements_are_everything_but_views(self):
        m = metrics.read_metrics(public(500, like_count=3, reply_count=2,
                                        retweet_count=1, bookmark_count=4))
        assert m.engagements == 10


class TestTheCheapEndpoint:
    """$0.001 a post against $0.005 for the same numbers."""

    def test_it_reads_the_timeline_not_the_posts_by_id(self, fake):
        collector = metrics.Collector(Cfg(), "12345")
        collector.fetch()

        assert fake.last.timeline_calls, "the timeline was never read"
        assert fake.last.timeline_calls[0]["id"] == "12345"

    def test_it_asks_for_public_metrics_only(self, fake):
        # non_public_metrics and organic_metrics hold the same view count but
        # only for posts under 30 days old, so they would start returning
        # nothing without any error.
        metrics.Collector(Cfg(), "12345").fetch()

        fields = fake.last.timeline_calls[0]["tweet_fields"]
        assert "public_metrics" in fields
        assert not any("non_public" in f or "organic" in f for f in fields)

    def test_the_bill_is_the_number_of_posts_returned(self, fake):
        FakeClient.pages = [page([tweet(str(i), i) for i in range(1, 8)])]

        collector = metrics.Collector(Cfg(), "12345")
        collector.fetch()
        assert collector.resources == 7


class TestPaging:
    def test_it_follows_the_next_token(self, fake):
        FakeClient.pages = [
            page([tweet("1", 10)], next_token="abc"),
            page([tweet("2", 20)]),
        ]

        found = metrics.Collector(Cfg(), "12345").fetch()

        assert set(found) == {"1", "2"}
        assert fake.last.timeline_calls[1]["pagination_token"] == "abc"

    def test_it_stops_once_the_page_is_older_than_the_window(self, fake):
        FakeClient.pages = [
            page([tweet("1", 10, age_days=1), tweet("2", 20, age_days=30)], next_token="abc"),
            page([tweet("3", 30, age_days=60)]),
        ]

        found = metrics.Collector(Cfg(), "12345").fetch(
            since=metrics.window_start(7, NOW)
        )

        assert set(found) == {"1", "2"}
        assert len(fake.last.timeline_calls) == 1, "it paid for a page it did not need"

    def test_paging_is_bounded(self, fake):
        FakeClient.pages = [page([tweet("1", 1)], next_token="forever")]

        collector = metrics.Collector(Cfg(), "12345")
        collector.fetch()
        assert collector.pages == metrics.MAX_PAGES

    def test_an_empty_timeline_is_not_an_error(self, fake):
        FakeClient.pages = [page([])]
        assert metrics.Collector(Cfg(), "12345").fetch() == {}


class TestFailures:
    def test_an_ignored_field_parameter_is_loud(self, fake):
        # The silent version of this bug logs zeros forever: a field selector
        # the API does not recognise returns id and text and no error.
        FakeClient.pages = [page([NS(id="1", public_metrics=None, created_at=NOW)])]

        with pytest.raises(metrics.MetricsError, match="public_metrics"):
            metrics.Collector(Cfg(), "12345").fetch()

    def test_a_rate_limit_is_a_metrics_error(self, fake, monkeypatch):
        def boom(**kwargs):
            raise tweepy.errors.TooManyRequests(
                NS(status_code=429, reason="", json=lambda: {}, headers={})
            )

        monkeypatch.setattr(FakeClient, "get_users_tweets", lambda self, **k: boom())
        with pytest.raises(metrics.MetricsError, match="rate limited"):
            metrics.Collector(Cfg(), "12345").fetch()

    def test_a_network_error_is_a_metrics_error(self, fake, monkeypatch):
        import requests

        def boom(self, **kwargs):
            raise requests.exceptions.ConnectTimeout("no route")

        monkeypatch.setattr(FakeClient, "get_users_tweets", boom)
        with pytest.raises(metrics.MetricsError, match="network"):
            metrics.Collector(Cfg(), "12345").fetch()


class TestTheStore:
    def _posts(self):
        return [
            PostRecord(text="about cement", topic="cement", tweet_id="1",
                       posted_at="2026-10-19T09:00:00+00:00"),
        ]

    def test_it_records_what_was_fetched(self):
        store = metrics.merge({}, {"1": metrics.PostMetrics(impressions=120, likes=4)},
                              self._posts(), NOW)

        record = store["posts"]["1"]
        assert record["impressions"] == 120
        assert record["likes"] == 4
        assert record["topic"] == "cement"
        assert record["text"] == "about cement"

    def test_a_post_outside_the_window_keeps_its_last_reading(self):
        store = {"posts": {"old": {"impressions": 900, "topic": "fans"}}}

        metrics.merge(store, {"1": metrics.PostMetrics(impressions=5)}, self._posts(), NOW)

        # Not refreshed, not deleted, not zeroed: the running total has to be
        # stable rather than a function of how far back the last run looked.
        assert store["posts"]["old"]["impressions"] == 900
        assert metrics.totals(store).impressions == 905

    def test_history_keeps_one_point_per_day(self):
        store = {}
        later = NOW + timedelta(hours=6)
        tomorrow = NOW + timedelta(days=1)

        metrics.merge(store, {"1": metrics.PostMetrics(impressions=10)}, [], NOW)
        metrics.merge(store, {"1": metrics.PostMetrics(impressions=40)}, [], later)
        metrics.merge(store, {"1": metrics.PostMetrics(impressions=90)}, [], tomorrow)

        assert store["posts"]["1"]["history"] == [
            ["2026-10-19", 40],
            ["2026-10-20", 90],
        ]

    def test_a_corrupt_file_does_not_lose_the_run(self, tmp_path):
        path = tmp_path / "metrics.json"
        path.write_text("{not json", encoding="utf-8")
        assert metrics.load(path) == metrics.EMPTY

    def test_a_round_trip(self, tmp_path):
        path = tmp_path / "metrics.json"
        store = metrics.merge({}, {"1": metrics.PostMetrics(impressions=7)}, [], NOW)

        metrics.save(path, store)
        assert metrics.load(path)["posts"]["1"]["impressions"] == 7
        assert not list(tmp_path.glob("*.tmp")), "the temp file was left behind"


class TestTheTable:
    def _store(self):
        store = {}
        metrics.merge(
            store,
            {"1": metrics.PostMetrics(impressions=412, likes=3),
             "2": metrics.PostMetrics(impressions=98)},
            [
                PostRecord(text="a | pipe in the text", topic="pipes", tweet_id="1",
                           posted_at="2026-10-19T09:00:00+00:00"),
                PostRecord(text="older", topic="old", tweet_id="2",
                           posted_at="2026-10-18T09:00:00+00:00"),
            ],
            NOW,
        )
        return store

    def test_it_totals_and_sorts_newest_first(self):
        table = metrics.render(self._store())

        assert "510 views" in table
        rows = [line for line in table.splitlines() if line.startswith("| 2026")]
        assert rows[0].startswith("| 2026-10-19")

    def test_a_pipe_in_a_post_does_not_break_the_table(self):
        assert r"a \| pipe in the text" in metrics.render(self._store())

    def test_it_names_the_best_post(self):
        assert "Best so far: 412" in metrics.render(self._store())

    def test_an_empty_store_still_renders(self):
        assert "0 posts tracked" in metrics.render({"posts": {}}).replace("\n", " ")


class TestTheAccountId:
    def test_the_setting_wins(self, fake):
        class WithId(Cfg):
            x_user_id = "999"

        assert track.account_id(WithId(), {}) == "999"

    def test_otherwise_the_cache(self, fake):
        assert track.account_id(Cfg(), {"x_user_id": "777"}) == "777"

    def test_otherwise_one_paid_lookup_that_is_then_cached(self, fake):
        # /2/users/me is a $0.010 read and is not an Owned Read, so paying
        # for it every run would cost more than the metrics do.
        state = {}
        assert track.account_id(Cfg(), state) == "12345"
        assert state["x_user_id"] == "12345"


class TestTheRunNeverGoesRed:
    """The posts are already out. Counting them is not worth a failure."""

    def _paths(self, tmp_path, monkeypatch):
        import bot.config as config

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        monkeypatch.setattr(config, "METRICS_PATH", memory_dir / "metrics.json")
        monkeypatch.setattr(config, "METRICS_MD_PATH", memory_dir / "METRICS.md")
        monkeypatch.setattr(config, "STATE_PATH", memory_dir / "state.json")
        monkeypatch.setattr(config, "POSTS_PATH", memory_dir / "posts.jsonl")
        for key, value in [
            ("ANTHROPIC_API_KEY", "sk-ant-test"), ("X_API_KEY", "k"),
            ("X_API_SECRET", "s"), ("X_ACCESS_TOKEN", "t"),
            ("X_ACCESS_TOKEN_SECRET", "ts"),
        ]:
            monkeypatch.setenv(key, value)
        for key in ("X_USER_ID", "METRICS_WINDOW_DAYS", "DRY_RUN"):
            monkeypatch.delenv(key, raising=False)
        return memory_dir

    def test_a_good_run_writes_both_files(self, fake, tmp_path, monkeypatch):
        memory_dir = self._paths(tmp_path, monkeypatch)
        (memory_dir / "posts.jsonl").write_text(
            json.dumps({"text": "hello", "tweet_id": "1", "topic": "t",
                        "posted_at": "2026-10-19T09:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        FakeClient.pages = [page([tweet("1", 250)])]

        assert track.run() == track.EXIT_OK
        assert json.loads((memory_dir / "metrics.json").read_text())["posts"]["1"][
            "impressions"
        ] == 250
        assert "250" in (memory_dir / "METRICS.md").read_text()

    def test_a_failed_collection_is_still_a_green_run(self, fake, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)

        def boom(self, **kwargs):
            raise tweepy.errors.TweepyException("X is down")

        monkeypatch.setattr(FakeClient, "get_users_tweets", boom)
        assert track.run() == track.EXIT_OK

    def test_missing_credentials_asks_for_a_human(self, fake, tmp_path, monkeypatch):
        self._paths(tmp_path, monkeypatch)
        monkeypatch.setenv("DRY_RUN", "true")  # lets the config load without them
        for key in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
                    "X_ACCESS_TOKEN_SECRET"):
            monkeypatch.delenv(key)

        assert track.run() == track.EXIT_NEEDS_HUMAN
