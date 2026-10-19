"""How many people actually saw it.

X bills reads per POST RETURNED, not per request, and the obvious way to
build this is five times more expensive than the right one. Reading
GET /2/tweets?ids=... costs $0.005 a post; reading your own timeline through
GET /2/users/{id}/tweets qualifies as an "Owned Read" at $0.001. Same
numbers, same field, one fifth the bill. The tweet ids in memory/posts.jsonl
are still the bookkeeping, but they are not how the numbers are fetched.

Two consequences of per-resource billing shape everything here:

Polling often is free; polling widely is not. Resources are deduplicated per
UTC calendar day, so checking the same post six times in a day costs what
checking it once costs. What costs money is how many DISTINCT posts get
touched. So this refreshes only posts still young enough to be moving, and
freezes the rest at their last known figure.

And the number itself is a snapshot, not a series. The API returns what the
count is now, never what it was. If anyone wants a graph, this file has to
keep the history, which it does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import tweepy

log = logging.getLogger(__name__)

# The view count. non_public_metrics and organic_metrics carry the same
# number, but only for posts under 30 days old and only with authorship, so
# they buy nothing here and break silently on anything older.
FIELDS = ["public_metrics", "created_at"]

# The most the timeline endpoint returns per page.
PAGE_SIZE = 100

# Pages per run. At 100 posts a page this is far more than the account will
# produce, and it stops a pagination bug from spending the balance.
MAX_PAGES = 5


@dataclass
class PostMetrics:
    impressions: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "impressions": self.impressions,
            "likes": self.likes,
            "replies": self.replies,
            "reposts": self.reposts,
            "quotes": self.quotes,
            "bookmarks": self.bookmarks,
        }

    @property
    def engagements(self) -> int:
        return self.likes + self.replies + self.reposts + self.quotes + self.bookmarks


class MetricsError(RuntimeError):
    """The numbers could not be read. Never fatal: they are an observation."""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_metrics(raw: Any) -> PostMetrics:
    """Pull the counts out of one post's public_metrics object."""
    if not isinstance(raw, dict):
        return PostMetrics()

    return PostMetrics(
        impressions=_int(raw.get("impression_count")),
        likes=_int(raw.get("like_count")),
        replies=_int(raw.get("reply_count")),
        # The spec renamed this to repost_count; the docs and older responses
        # still say retweet_count. Read whichever turns up.
        reposts=_int(raw.get("repost_count", raw.get("retweet_count"))),
        quotes=_int(raw.get("quote_count")),
        bookmarks=_int(raw.get("bookmark_count")),
    )


class Collector:
    """Fetches the account's own timeline and reports what it cost."""

    def __init__(self, cfg, user_id: str) -> None:
        self.cfg = cfg
        self.user_id = user_id
        self.resources = 0  # posts returned, which is what X bills
        self.pages = 0

        self.client = tweepy.Client(
            consumer_key=cfg.x_api_key,
            consumer_secret=cfg.x_api_secret,
            access_token=cfg.x_access_token,
            access_token_secret=cfg.x_access_token_secret,
            wait_on_rate_limit=False,
        )

    def fetch(self, since: datetime | None = None) -> dict[str, PostMetrics]:
        """Every post on the timeline, newest first, until `since`."""
        found: dict[str, PostMetrics] = {}
        token = None

        for _ in range(MAX_PAGES):
            page = self._page(token)
            self.pages += 1

            posts = list(getattr(page, "data", None) or [])
            if not posts:
                break

            self.resources += len(posts)
            oldest_seen = None

            for post in posts:
                raw = getattr(post, "public_metrics", None)
                if raw is None and self.pages == 1:
                    # A field selector the API ignored returns id and text and
                    # nothing else, and a tracker built on that logs zeros
                    # forever rather than failing. Say so loudly, once.
                    raise MetricsError(
                        "the response carried no public_metrics; the field "
                        "parameter was probably ignored"
                    )
                found[str(post.id)] = read_metrics(raw)
                oldest_seen = getattr(post, "created_at", None) or oldest_seen

            # The timeline is newest first, so once a page ends older than
            # the window there is nothing left worth paying for.
            if _older_than(oldest_seen, since):
                break

            token = _next_token(page)
            if not token:
                break

        return found

    def _page(self, token: str | None):
        try:
            return self.client.get_users_tweets(
                id=self.user_id,
                tweet_fields=FIELDS,
                max_results=PAGE_SIZE,
                pagination_token=token,
                user_auth=True,
            )
        except tweepy.errors.TooManyRequests as exc:
            raise MetricsError(f"rate limited reading the timeline: {exc}") from exc
        except tweepy.TweepyException as exc:
            raise MetricsError(f"could not read the timeline: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - requests errors are not Tweepy's
            raise MetricsError(f"network problem reading the timeline: {exc}") from exc


def _next_token(page: Any) -> str | None:
    meta = getattr(page, "meta", None)
    return meta.get("next_token") if isinstance(meta, dict) else None


def _older_than(seen: Any, since: datetime | None) -> bool:
    """Has the timeline reached past the refresh window?

    created_at arrives as a datetime from tweepy, but a naive one would raise
    on comparison rather than merely read oddly, and a page that cannot be
    dated is not a reason to stop paginating blind.
    """
    if since is None or not isinstance(seen, datetime):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen < since


# ------------------------------------------------------------------ the file

EMPTY: dict[str, Any] = {"updated_at": None, "posts": {}}


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(EMPTY))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s); starting fresh", path, exc)
        return json.loads(json.dumps(EMPTY))

    if not isinstance(data, dict) or not isinstance(data.get("posts"), dict):
        return json.loads(json.dumps(EMPTY))
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    """Atomic, like every other write into memory/: the file is committed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def merge(
    store: dict[str, Any],
    fetched: dict[str, PostMetrics],
    posts: Iterable,
    now: datetime,
) -> dict[str, Any]:
    """Fold today's numbers in, keeping the last known figure for the rest.

    A post that has aged out of the refresh window is not deleted and not
    zeroed: its final reading stays, which is what makes the running total
    stable rather than a function of how far back the last run happened to
    look.
    """
    known = store.setdefault("posts", {})
    by_id = {str(p.tweet_id): p for p in posts if p.tweet_id}
    day = now.date().isoformat()

    for tweet_id, metrics in fetched.items():
        record = known.setdefault(tweet_id, {})
        record.update(metrics.as_dict())
        record["checked_at"] = now.isoformat(timespec="seconds")

        post = by_id.get(tweet_id)
        if post is not None:
            record["posted_at"] = post.posted_at
            record["topic"] = post.topic
            record["text"] = post.text

        # One point per UTC day, so the series is a graph rather than a log of
        # every poll, and the file stays small enough to commit forever.
        history = record.setdefault("history", [])
        if history and history[-1][0] == day:
            history[-1] = [day, metrics.impressions]
        else:
            history.append([day, metrics.impressions])

    store["updated_at"] = now.isoformat(timespec="seconds")
    return store


def totals(store: dict[str, Any]) -> PostMetrics:
    out = PostMetrics()
    for record in store.get("posts", {}).values():
        out.impressions += _int(record.get("impressions"))
        out.likes += _int(record.get("likes"))
        out.replies += _int(record.get("replies"))
        out.reposts += _int(record.get("reposts"))
        out.quotes += _int(record.get("quotes"))
        out.bookmarks += _int(record.get("bookmarks"))
    return out


def window_start(days: int, now: datetime) -> datetime | None:
    """Oldest post worth paying to refresh, or None for all of them."""
    if days <= 0:
        return None
    return now - timedelta(days=days)


def render(store: dict[str, Any], limit: int = 50) -> str:
    """The table a person reads, newest first."""
    records = sorted(
        store.get("posts", {}).items(),
        key=lambda item: str(item[1].get("posted_at") or ""),
        reverse=True,
    )
    summed = totals(store)
    counted = len(records)

    lines = [
        "# Views",
        "",
        f"Updated {store.get('updated_at') or 'never'}. "
        f"{counted} post{'s' if counted != 1 else ''} tracked.",
        "",
        f"**{summed.impressions:,} views** · {summed.likes:,} likes · "
        f"{summed.replies:,} replies · {summed.reposts:,} reposts · "
        f"{summed.bookmarks:,} bookmarks",
        "",
    ]

    if counted:
        average = summed.impressions / counted
        best = max(records, key=lambda item: _int(item[1].get("impressions")))
        lines += [
            f"Average {average:,.0f} views a post. "
            f"Best so far: {_int(best[1].get('impressions')):,} "
            f"({best[1].get('topic') or 'untitled'}).",
            "",
        ]

    lines += [
        "| Posted | Views | Likes | Replies | Reposts | Post |",
        "|---|---:|---:|---:|---:|---|",
    ]

    for tweet_id, record in records[:limit]:
        text = str(record.get("text") or "").replace("|", "\\|").replace("\n", " ")
        if len(text) > 90:
            text = text[:87] + "..."
        posted = str(record.get("posted_at") or "")[:16].replace("T", " ")
        lines.append(
            f"| {posted} "
            f"| {_int(record.get('impressions')):,} "
            f"| {_int(record.get('likes')):,} "
            f"| {_int(record.get('replies')):,} "
            f"| {_int(record.get('reposts')):,} "
            f"| [{text}](https://x.com/i/web/status/{tweet_id}) |"
        )

    if counted > limit:
        lines.append("")
        lines.append(f"_{counted - limit} older posts not shown._")

    lines.append("")
    return "\n".join(lines)
