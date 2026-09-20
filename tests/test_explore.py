import json
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import bot.explore as explore
from bot.explore import (
    FETCH_TIMEOUT,
    FeedCache,
    FeedItem,
    Source,
    build_digest,
    gather,
    load_sources,
    socket_timeout,
)


class TestLoadSources:
    def test_parses_name_and_url(self, tmp_path):
        path = tmp_path / "feeds.txt"
        path.write_text(
            "# a comment\n"
            "\n"
            "Hacker News | https://hnrss.org/frontpage\n"
            "https://example.com/feed.xml\n",
            encoding="utf-8",
        )
        sources = load_sources(path)
        assert [s.name for s in sources] == ["Hacker News", "example.com"]
        assert sources[0].url == "https://hnrss.org/frontpage"

    def test_ignores_malformed_lines(self, tmp_path):
        path = tmp_path / "feeds.txt"
        path.write_text(
            "Good | https://example.com/a\n"
            "Bad | not-a-url\n"
            "Also bad | ftp://example.com/b\n",
            encoding="utf-8",
        )
        assert [s.name for s in load_sources(path)] == ["Good"]

    def test_missing_file_is_empty(self, tmp_path):
        assert load_sources(tmp_path / "nope.txt") == []

    def test_the_shipped_feed_list_parses(self):
        from bot.config import FEEDS_PATH

        sources = load_sources(FEEDS_PATH)
        assert len(sources) >= 20
        assert all(s.url.startswith("https://") for s in sources)
        assert len({s.url for s in sources}) == len(sources), "duplicate feed URLs"


class TestFeedCache:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "feed_cache.json"
        cache = FeedCache(path)
        cache.put("https://a.com/feed", "etag-1", "Mon, 01 Jan 2026 00:00:00 GMT")
        cache.save()

        reloaded = FeedCache(path)
        assert reloaded.get("https://a.com/feed") == (
            "etag-1",
            "Mon, 01 Jan 2026 00:00:00 GMT",
        )

    def test_unknown_url_is_empty(self, tmp_path):
        assert FeedCache(tmp_path / "c.json").get("https://nope") == (None, None)

    def test_nothing_stored_when_headers_absent(self, tmp_path):
        path = tmp_path / "c.json"
        cache = FeedCache(path)
        cache.put("https://a.com/feed", None, None)
        cache.save()
        assert FeedCache(path).get("https://a.com/feed") == (None, None)

    def test_corrupt_cache_does_not_crash(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{ not json", encoding="utf-8")
        assert FeedCache(path).get("https://a.com") == (None, None)

    def test_prune_drops_removed_feeds(self, tmp_path):
        path = tmp_path / "c.json"
        cache = FeedCache(path)
        cache.put("https://keep.com/f", "e1", None)
        cache.put("https://drop.com/f", "e2", None)
        cache.prune({"https://keep.com/f"})
        cache.save()

        reloaded = FeedCache(path)
        assert reloaded.get("https://keep.com/f") == ("e1", None)
        assert reloaded.get("https://drop.com/f") == (None, None)


def _fake_parsed(status=200, entries=(), etag=None, modified=None):
    return SimpleNamespace(
        status=status,
        entries=list(entries),
        bozo=0,
        etag=etag,
        modified=modified,
    )


class TestFetchSource:
    def test_304_returns_nothing_and_keeps_cache(self, tmp_path, monkeypatch):
        cache = FeedCache(tmp_path / "c.json")
        cache.put("https://a.com/feed", "etag-1", None)
        monkeypatch.setattr(
            explore.feedparser, "parse", lambda *a, **k: _fake_parsed(status=304)
        )

        items = explore.fetch_source(Source("A", "https://a.com/feed"), 5, cache)
        assert items == []
        assert cache.get("https://a.com/feed") == ("etag-1", None)

    def test_sends_stored_validators(self, tmp_path, monkeypatch):
        cache = FeedCache(tmp_path / "c.json")
        cache.put("https://a.com/feed", "etag-1", "Mon, 01 Jan 2026 00:00:00 GMT")
        seen = {}

        def fake_parse(url, **kwargs):
            seen.update(kwargs)
            return _fake_parsed()

        monkeypatch.setattr(explore.feedparser, "parse", fake_parse)
        explore.fetch_source(Source("A", "https://a.com/feed"), 5, cache)

        assert seen["etag"] == "etag-1"
        assert seen["modified"] == "Mon, 01 Jan 2026 00:00:00 GMT"

    def test_http_error_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(
            explore.feedparser, "parse", lambda *a, **k: _fake_parsed(status=404)
        )
        assert explore.fetch_source(Source("A", "https://a.com/f"), 5) == []

    def test_network_exception_is_swallowed(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection reset")

        monkeypatch.setattr(explore.feedparser, "parse", boom)
        assert explore.fetch_source(Source("A", "https://a.com/f"), 5) == []

    def test_html_is_stripped_from_summaries(self, monkeypatch):
        entry = {
            "title": "A &amp; B",
            "link": "https://a.com/1",
            "summary": "<p>Some <b>bold</b> text &mdash; here</p>",
        }
        monkeypatch.setattr(
            explore.feedparser, "parse", lambda *a, **k: _fake_parsed(entries=[entry])
        )
        items = explore.fetch_source(Source("A", "https://a.com/f"), 5)
        assert items[0].title == "A & B"
        assert "<b>" not in items[0].summary
        assert "bold" in items[0].summary

    def test_respects_items_per_source(self, monkeypatch):
        entries = [{"title": f"t{i}", "link": f"https://a.com/{i}"} for i in range(20)]
        monkeypatch.setattr(
            explore.feedparser, "parse", lambda *a, **k: _fake_parsed(entries=entries)
        )
        assert len(explore.fetch_source(Source("A", "https://a.com/f"), 3)) == 3

    def test_entries_without_titles_are_skipped(self, monkeypatch):
        entries = [{"link": "https://a.com/1"}, {"title": "real", "link": "https://a.com/2"}]
        monkeypatch.setattr(
            explore.feedparser, "parse", lambda *a, **k: _fake_parsed(entries=entries)
        )
        items = explore.fetch_source(Source("A", "https://a.com/f"), 5)
        assert [i.title for i in items] == ["real"]


class TestGatherAndDigest:
    def test_one_prolific_feed_cannot_dominate(self, monkeypatch):
        def fake_fetch(source, items_per_source, cache=None):
            count = 10 if source.name == "Loud" else 1
            return [
                FeedItem(source=source.name, title=f"{source.name} {i}", link="")
                for i in range(count)
            ]

        monkeypatch.setattr(explore, "fetch_source", fake_fetch)
        sources = [Source("Loud", "https://l.com/f"), Source("Quiet", "https://q.com/f")]
        items = gather(sources, sample_size=2, items_per_source=10)

        # The quiet feed's single item must appear within the first two slots,
        # not be buried under ten from the loud one.
        assert "Quiet" in {i.source for i in items[:2]}
        assert len(items) == 11

    def test_empty_sources_is_empty(self):
        assert gather([], sample_size=5, items_per_source=5) == []

    def test_sample_size_zero_disables_the_digest(self):
        # Regression: ThreadPoolExecutor(max_workers=0) raises ValueError, so
        # FEED_SAMPLE_SIZE=0 used to crash the run rather than turn feeds off.
        sources = [Source("A", "https://a.com/f"), Source("B", "https://b.com/f")]
        assert gather(sources, sample_size=0, items_per_source=5) == []

    def test_negative_sample_size_is_clamped(self):
        assert gather([Source("A", "https://a.com/f")], sample_size=-3, items_per_source=5) == []

    def test_digest_is_empty_for_no_items(self):
        assert build_digest([]) == ""

    def test_digest_includes_source_title_and_link(self):
        item = FeedItem(
            source="Quanta",
            title="A proof arrives",
            link="https://q.com/1",
            summary="Short summary.",
        )
        text = build_digest([item])
        assert "[Quanta]" in text
        assert "A proof arrives" in text
        assert "https://q.com/1" in text

    def test_digest_respects_max_items(self):
        items = [FeedItem(source="S", title=f"t{i}", link="") for i in range(100)]
        assert build_digest(items, max_items=5).count("[S]") == 5


class TestSocketTimeout:
    """feedparser takes no timeout, so the process-wide default is the only
    lever. Without it a silent feed server pins a worker until the job's
    15 minute ceiling kills the run."""

    def test_sets_and_restores_the_default(self):
        before = socket.getdefaulttimeout()
        with socket_timeout(7.5):
            assert socket.getdefaulttimeout() == 7.5
        assert socket.getdefaulttimeout() == before

    def test_restores_even_when_the_body_raises(self):
        before = socket.getdefaulttimeout()
        with pytest.raises(ValueError):
            with socket_timeout(7.5):
                raise ValueError("boom")
        assert socket.getdefaulttimeout() == before

    def test_gather_applies_it_while_fetching(self, monkeypatch):
        seen = []

        def fake_fetch(source, items_per_source, cache=None):
            seen.append(socket.getdefaulttimeout())
            return []

        monkeypatch.setattr(explore, "fetch_source", fake_fetch)
        before = socket.getdefaulttimeout()

        gather([Source("A", "https://a.com/f")], sample_size=1, items_per_source=3)

        assert seen == [FETCH_TIMEOUT], "feeds were fetched without a timeout"
        assert socket.getdefaulttimeout() == before, "timeout leaked past the fetch"

    def test_gather_restores_it_when_a_worker_explodes(self, monkeypatch):
        def boom(source, items_per_source, cache=None):
            raise RuntimeError("socket exploded")

        monkeypatch.setattr(explore, "fetch_source", boom)
        before = socket.getdefaulttimeout()

        items = gather([Source("A", "https://a.com/f")], sample_size=1, items_per_source=3)

        assert items == []
        assert socket.getdefaulttimeout() == before

    def test_a_worker_thread_inherits_the_timeout(self):
        """The timeout is process-wide, so threads started inside the block
        must see it. This is the property the fix actually depends on."""
        observed = []

        with socket_timeout(3.25):
            thread = threading.Thread(target=lambda: observed.append(socket.getdefaulttimeout()))
            thread.start()
            thread.join()

        assert observed == [3.25]
