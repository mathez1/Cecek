"""Raw material: a rotating sample of public RSS/Atom feeds.

This is not the interesting part of the bot. It exists so the model starts each
run with a rough sense of what the world is talking about, cheaply and without
any API key. The model is explicitly told it may ignore all of it.

Feeds are sampled rather than exhausted: pulling a different dozen each hour
keeps runs fast, keeps the prompt small, and stops the account from orbiting
whichever source posts most often.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; CecekBot/1.0; +https://github.com/mathez1/Cecek)"
)
FETCH_TIMEOUT = 15
MAX_ITEM_AGE = timedelta(hours=48)
SUMMARY_CHARS = 240

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FeedItem:
    source: str
    title: str
    link: str
    summary: str = ""
    published: datetime | None = None

    def render(self) -> str:
        line = f"[{self.source}] {self.title}"
        if self.summary:
            line += f"\n    {self.summary}"
        if self.link:
            line += f"\n    {self.link}"
        return line


@dataclass
class Source:
    name: str
    url: str


def load_sources(path: Path) -> list[Source]:
    """Parse prompts/feeds.txt. Lines are `Name | https://...`, blanks and # ignored."""
    if not path.exists():
        log.warning("no feed list at %s", path)
        return []

    sources: list[Source] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, _, url = line.partition("|")
            name, url = name.strip(), url.strip()
        else:
            url = line
            name = _name_from_url(url)
        if url.startswith(("http://", "https://")):
            sources.append(Source(name=name or _name_from_url(url), url=url))
        else:
            log.warning("ignoring malformed feed line: %s", raw)

    return sources


def _name_from_url(url: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return host


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _published_of(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None) or entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


class FeedCache:
    """Remembers each feed's ETag and Last-Modified across runs.

    Sending them back turns most of the bot's polling into empty 304 responses.
    It is the main thing that separates a well-behaved feed reader from one that
    re-downloads the same megabytes every hour and gets throttled on merit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = {
                        k: v for k, v in loaded.items() if isinstance(v, dict)
                    }
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not read the feed cache (%s); starting fresh", exc)

    def get(self, url: str) -> tuple[str | None, str | None]:
        entry = self._data.get(url, {})
        return entry.get("etag"), entry.get("modified")

    def put(self, url: str, etag: str | None, modified: str | None) -> None:
        if etag or modified:
            entry = {}
            if etag:
                entry["etag"] = etag
            if modified:
                entry["modified"] = modified
            self._data[url] = entry

    def prune(self, live_urls: set[str]) -> None:
        for url in list(self._data):
            if url not in live_urls:
                del self._data[url]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not save the feed cache: %s", exc)


def fetch_source(
    source: Source, items_per_source: int, cache: FeedCache | None = None
) -> list[FeedItem]:
    """Fetch one feed. Never raises: a dead feed just contributes nothing."""
    etag, modified = cache.get(source.url) if cache else (None, None)

    try:
        parsed = feedparser.parse(
            source.url,
            agent=USER_AGENT,
            etag=etag,
            modified=modified,
            request_headers={"Accept": "application/rss+xml, application/atom+xml, */*"},
        )
    except Exception as exc:  # feedparser can surface almost anything from the socket layer
        log.warning("failed to fetch %s: %s", source.name, exc)
        return []

    status = getattr(parsed, "status", None)

    if status == 304:
        log.info("%s unchanged since last fetch", source.name)
        return []

    if status is not None and status >= 400:
        log.warning("%s returned HTTP %s", source.name, status)
        return []

    if cache is not None:
        cache.put(
            source.url,
            getattr(parsed, "etag", None),
            getattr(parsed, "modified", None),
        )

    # bozo just means the XML was imperfect. feedparser usually recovers, so
    # only give up when it actually produced nothing.
    entries = getattr(parsed, "entries", []) or []
    if not entries:
        if getattr(parsed, "bozo", 0):
            log.warning(
                "%s was unparseable: %s", source.name, getattr(parsed, "bozo_exception", "")
            )
        else:
            log.info("%s had no entries", source.name)
        return []

    cutoff = datetime.now(timezone.utc) - MAX_ITEM_AGE
    items: list[FeedItem] = []

    for entry in entries:
        title = _clean(entry.get("title"))
        if not title:
            continue

        published = _published_of(entry)
        if published and published < cutoff:
            continue

        summary = _clean(entry.get("summary") or entry.get("description"))
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "..."

        items.append(
            FeedItem(
                source=source.name,
                title=title,
                link=(entry.get("link") or "").strip(),
                summary=summary,
                published=published,
            )
        )
        if len(items) >= items_per_source:
            break

    log.info("%s: %d items", source.name, len(items))
    return items


def gather(
    sources: list[Source],
    *,
    sample_size: int,
    items_per_source: int,
    rng: random.Random | None = None,
    cache: FeedCache | None = None,
) -> list[FeedItem]:
    """Fetch a random sample of the feeds, concurrently."""
    if not sources:
        return []

    rng = rng or random.Random()
    sample = rng.sample(sources, k=min(sample_size, len(sources)))
    log.info("sampling %d of %d feeds", len(sample), len(sources))

    items: list[FeedItem] = []
    with ThreadPoolExecutor(max_workers=min(8, len(sample))) as pool:
        futures = {
            pool.submit(fetch_source, src, items_per_source, cache): src
            for src in sample
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                items.extend(future.result())
            except Exception as exc:
                log.warning("unexpected error fetching %s: %s", src.name, exc)

    # Interleave sources so one prolific feed cannot dominate the digest.
    by_source: dict[str, list[FeedItem]] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)
    for bucket in by_source.values():
        rng.shuffle(bucket)

    ordered: list[FeedItem] = []
    while by_source:
        for name in list(by_source):
            bucket = by_source[name]
            ordered.append(bucket.pop())
            if not bucket:
                del by_source[name]

    return ordered


def build_digest(items: list[FeedItem], max_items: int = 45) -> str:
    if not items:
        return ""
    return "\n".join(item.render() for item in items[:max_items])
