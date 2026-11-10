"""BoB answers the people who talk to him. Nobody else.

Since February 2026 the X API refuses a reply unless the post being answered
@mentions this account or quotes one of its posts, and X's automation rules
forbid unsolicited automated replies anyway. So this reads the account's
mentions and answers those, and that is the whole of its reach. It never
searches for conversations to join.

Separate from the poster for the same reason the view tracker is: posting is
the thing that must not fail. A reply that cannot be written or sent costs a
reply. It never costs a post, never turns a run red, and never touches the
circuit breaker.

Three limits, each for a different failure:
  * once per post, ever: X's rules allow one automated reply to a post
  * a few per person per day: two bots answering each other loop forever
  * a few per run and a cap per day: a pile-on should not become a bill
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import tweepy

from bot import guard, memory, track
from bot.brain import Brain, BrainError
from bot.config import POST_CHAR_LIMIT, ConfigError, load_config
from bot.main import EXIT_OK, load_persona, setup_logging, summary
from bot.x_client import XClient, XError, XQuotaError, XRateLimitError

log = logging.getLogger(__name__)

# Replies to the same person in one day. A real conversation rarely needs
# more; another bot replying to every reply needs exactly this cap.
MAX_PER_AUTHOR_PER_DAY = 3

# On the very first read there is no since_id, and the endpoint would hand
# back the account's whole mention history. Old mentions are not worth a
# reply that arrives weeks late, so the first read only looks back a day.
FIRST_READ_LOOKBACK = timedelta(days=1)

FIELDS = ["author_id", "conversation_id", "created_at", "referenced_tweets"]


@dataclass
class Mention:
    id: str
    author_id: str
    text: str
    created_at: datetime | None = None
    replied_to: str | None = None  # the post this one answers, if any
    is_retweet: bool = False


class MentionsError(RuntimeError):
    """The mentions could not be read. Never fatal."""


def _references(tweet: Any) -> tuple[str | None, bool]:
    replied_to, retweet = None, False
    for ref in getattr(tweet, "referenced_tweets", None) or []:
        kind = ref.get("type") if isinstance(ref, dict) else getattr(ref, "type", None)
        ref_id = ref.get("id") if isinstance(ref, dict) else getattr(ref, "id", None)
        if kind == "replied_to":
            replied_to = str(ref_id)
        elif kind == "retweeted":
            retweet = True
    return replied_to, retweet


def fetch_mentions(cfg, user_id: str, since_id: str | None) -> tuple[list[Mention], dict[str, str]]:
    """Newest mentions after since_id, oldest first, plus quoted-post texts."""
    client = tweepy.Client(
        consumer_key=cfg.x_api_key,
        consumer_secret=cfg.x_api_secret,
        access_token=cfg.x_access_token,
        access_token_secret=cfg.x_access_token_secret,
        wait_on_rate_limit=False,
    )
    kwargs: dict[str, Any] = dict(
        id=user_id,
        max_results=20,
        tweet_fields=FIELDS,
        expansions=["referenced_tweets.id"],
        user_auth=True,
    )
    if since_id:
        kwargs["since_id"] = since_id
    else:
        kwargs["start_time"] = datetime.now(timezone.utc) - FIRST_READ_LOOKBACK

    try:
        response = client.get_users_mentions(**kwargs)
    except tweepy.TweepyException as exc:
        raise MentionsError(f"could not read mentions: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - requests errors are not Tweepy's
        raise MentionsError(f"network problem reading mentions: {exc}") from exc

    mentions = []
    for tweet in getattr(response, "data", None) or []:
        replied_to, retweet = _references(tweet)
        mentions.append(Mention(
            id=str(tweet.id),
            author_id=str(getattr(tweet, "author_id", "") or ""),
            text=str(getattr(tweet, "text", "") or ""),
            created_at=getattr(tweet, "created_at", None),
            replied_to=replied_to,
            is_retweet=retweet,
        ))

    includes = getattr(response, "includes", None) or {}
    referenced = {
        str(t.id): str(getattr(t, "text", "") or "")
        for t in (includes.get("tweets") or [])
    }
    # The endpoint returns newest first; answer in the order people wrote.
    mentions.sort(key=lambda m: int(m.id) if m.id.isdigit() else 0)
    return mentions, referenced


def _author_count(state: dict, author_id: str, day: str) -> int:
    return int(state.get("reply_authors", {}).get(day, {}).get(author_id, 0))


def _count_author(state: dict, author_id: str, day: str) -> None:
    authors = state.setdefault("reply_authors", {})
    for old in [d for d in authors if d != day]:
        del authors[old]
    today = authors.setdefault(day, {})
    today[author_id] = int(today.get(author_id, 0)) + 1


def worth_answering(mention: Mention, user_id: str, state: dict, day: str) -> str | None:
    """Why this mention gets no reply, or None if it may get one."""
    if mention.author_id == user_id:
        return "it is BoB's own post"
    if mention.is_retweet:
        return "it is a repost, not someone talking to BoB"
    if mention.id in set(state.get("replied", [])):
        return "already answered"
    if _author_count(state, mention.author_id, day) >= MAX_PER_AUTHOR_PER_DAY:
        return "already answered this person enough today"
    if not mention.text.strip():
        return "empty"
    return None


def check_reply(text: str, cfg) -> list[str]:
    """The post guard, plus the one rule that is stricter for replies."""
    result = guard.check_draft(
        text,
        [],
        char_limit=POST_CHAR_LIMIT,
        banned_openers=cfg.banned_openers,
        similarity_threshold=cfg.similarity_threshold,
        allow_links=cfg.allow_links,
    )
    # The post guard's minimum length is right for a post and wrong for a
    # reply: "You're right, thank you." is a complete answer at 24.
    problems = [p for p in result.problems if not p.startswith("too short")]
    # A hashtag in a reply reads as spam whatever it is about.
    if guard._HASHTAG.search(text):
        problems.append("a reply must not carry a hashtag")
    return problems


def run() -> int:
    setup_logging()
    from bot.config import PERSONA_PATH, STATE_PATH

    try:
        cfg = load_config()
    except ConfigError as exc:
        # The posting step reports configuration problems; saying it twice
        # would only add noise.
        log.info("replies skipped: %s", exc)
        return EXIT_OK

    if not cfg.enable_replies:
        log.info("replies are switched off")
        return EXIT_OK
    if not cfg.has_x_credentials:
        log.info("no X credentials; no replies")
        return EXIT_OK

    state = memory.load_state(STATE_PATH)
    now = datetime.now(timezone.utc)
    day = memory.day_key(now)

    room = cfg.daily_reply_budget - memory.replies_today(state, now)
    if cfg.daily_reply_budget > 0 and room <= 0:
        log.info("daily reply budget reached")
        return EXIT_OK
    per_run = cfg.max_replies_per_run if cfg.max_replies_per_run > 0 else 0
    allowance = min(per_run, room) if cfg.daily_reply_budget > 0 else per_run
    if allowance <= 0:
        return EXIT_OK

    try:
        user_id = track.account_id(cfg, state)
        mentions, referenced = fetch_mentions(cfg, user_id, state.get("mentions_since_id"))
    except (MentionsError, track.metrics.MetricsError) as exc:
        log.warning("%s", exc)
        summary(f"## Replies\n\nMentions not read: {exc}\n")
        _save(STATE_PATH, state, cfg)
        return EXIT_OK

    if not mentions:
        log.info("no new mentions")
        _save(STATE_PATH, state, cfg)
        return EXIT_OK

    persona = load_persona(PERSONA_PATH)
    brain = Brain(cfg)
    client = XClient(cfg) if not cfg.dry_run else None
    posted, silent, considered = [], [], 0

    for mention in mentions:
        if considered >= allowance:
            # Stop without moving since_id past what was not looked at, so
            # the next run picks the rest up.
            break

        reason = worth_answering(mention, user_id, state, day)
        if reason:
            log.info("not replying to %s: %s", mention.id, reason)
            state["mentions_since_id"] = mention.id
            continue

        considered += 1
        context = referenced.get(mention.replied_to or "", "")
        try:
            draft = brain.compose_reply(mention.text, context, persona)
        except BrainError as exc:
            log.warning("could not write a reply to %s: %s", mention.id, exc)
            state["mentions_since_id"] = mention.id
            continue

        if draft.skip or not draft.text:
            silent.append(f"{mention.id}: {draft.reason or 'nothing to add'}")
            state["mentions_since_id"] = mention.id
            continue

        problems = check_reply(draft.text, cfg)
        if problems:
            silent.append(f"{mention.id}: draft rejected ({'; '.join(problems)})")
            state["mentions_since_id"] = mention.id
            continue

        if cfg.dry_run:
            posted.append(f"(dry run) to {mention.id}: {draft.text}")
            continue

        try:
            result = client.post(draft.text, reply_to=mention.id)
        except (XRateLimitError, XQuotaError) as exc:
            # These clear by themselves, or need credit. Either way, stop and
            # leave since_id where it is so nothing is lost.
            log.warning("stopping replies: %s", exc)
            break
        except XError as exc:
            # Usually X declining this particular reply. Not worth retrying
            # forever, so it counts as handled.
            log.warning("X would not take the reply to %s: %s", mention.id, exc)
            silent.append(f"{mention.id}: X refused ({exc})")
            state["mentions_since_id"] = mention.id
            continue

        memory.record_reply(state, mention.id, now)
        _count_author(state, mention.author_id, day)
        state["mentions_since_id"] = mention.id
        posted.append(f"[{draft.text}]({result.url})")

    memory.record_spend(state, brain.meter.cost(cfg.model))
    _save(STATE_PATH, state, cfg)

    lines = ["## Replies", ""]
    lines += [f"- replied: {p}" for p in posted] or ["- no replies sent"]
    lines += [f"- stayed silent on {s}" for s in silent]
    summary("\n".join(lines) + "\n")
    return EXIT_OK


def _save(path, state: dict, cfg) -> None:
    if cfg.dry_run:
        return
    try:
        memory.save_state(path, state)
    except OSError as exc:
        log.warning("could not save state: %s", exc)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
