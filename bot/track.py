"""Collect the view counts. Run by its own workflow, not by the poster.

Separate from bot/main.py deliberately. Posting is the thing that must not
fail; counting is an observation about posts that already exist, and tying
the two together would mean a rate limit on the cheap half could cost a
post. They share memory/ and nothing else.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

import tweepy

from bot import memory, metrics
from bot.config import ConfigError, load_config
from bot.main import EXIT_NEEDS_HUMAN, EXIT_OK, setup_logging, summary

log = logging.getLogger(__name__)


def account_id(cfg, state: dict) -> str:
    """The numeric id, from the setting, the cache, or one paid lookup.

    GET /2/users/me is a $0.010 read and is not on the Owned Read list, so
    paying for it on every run would cost more than the metrics do. It never
    changes, so it is cached in state.json after the first time.
    """
    if cfg.x_user_id:
        return cfg.x_user_id

    cached = str(state.get("x_user_id") or "")
    if cached:
        return cached

    client = tweepy.Client(
        consumer_key=cfg.x_api_key,
        consumer_secret=cfg.x_api_secret,
        access_token=cfg.x_access_token,
        access_token_secret=cfg.x_access_token_secret,
        wait_on_rate_limit=False,
    )
    try:
        me = client.get_me(user_auth=True)
    except Exception as exc:  # noqa: BLE001 - tweepy and requests both raise here
        raise metrics.MetricsError(f"could not resolve the account id: {exc}") from exc

    found = str(getattr(getattr(me, "data", None), "id", "") or "")
    if not found:
        raise metrics.MetricsError("the account lookup returned no id")

    log.info("resolved and cached the account id")
    state["x_user_id"] = found
    return found


def run() -> int:
    setup_logging()

    # Imported here rather than at module scope so the paths stay a single
    # source of truth that a test can redirect, which is the same reason
    # bot/main.py does it.
    from bot.config import METRICS_MD_PATH, METRICS_PATH, POSTS_PATH, STATE_PATH

    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        summary(f"## Cannot collect views\n\n> {exc}\n")
        return EXIT_NEEDS_HUMAN

    if not cfg.has_x_credentials:
        log.error("no X credentials; nothing to read")
        summary("## Cannot collect views\n\nThe X credentials are not set.\n")
        return EXIT_NEEDS_HUMAN

    state = memory.load_state(STATE_PATH)
    posts = memory.load_posts(POSTS_PATH)
    store = metrics.load(METRICS_PATH)
    now = datetime.now(timezone.utc)

    try:
        user_id = account_id(cfg, state)
        collector = metrics.Collector(cfg, user_id)
        fetched = collector.fetch(since=metrics.window_start(cfg.metrics_window_days, now))
    except metrics.MetricsError as exc:
        # Nothing here is worth a red run: the posts are unaffected and the
        # next collection picks up the same numbers, slightly later.
        log.warning("could not collect views: %s", exc)
        summary(f"## Views not collected\n\n> {exc}\n\nThe next run will try again.\n")
        return EXIT_OK

    metrics.merge(store, fetched, posts, now)
    metrics.save(METRICS_PATH, store)

    try:
        METRICS_MD_PATH.write_text(metrics.render(store), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the table: %s", exc)

    try:
        memory.save_state(STATE_PATH, state)
    except OSError as exc:
        log.warning("could not save state: %s", exc)

    total = metrics.totals(store)
    log.info(
        "%d posts refreshed, %d tracked, %d views in total",
        len(fetched), len(store.get("posts", {})), total.impressions,
    )

    # Billed per post returned, so the resource count IS the bill. Printed
    # rather than assumed, because the rate is the thing most likely to move.
    cost = collector.resources * 0.001
    summary(
        f"## Views\n\n**{total.impressions:,} views** across "
        f"{len(store.get('posts', {}))} posts · {total.likes:,} likes · "
        f"{total.replies:,} replies · {total.reposts:,} reposts\n\n"
        f"Refreshed {len(fetched)} post(s) in {collector.pages} page(s). "
        f"Owned reads: {collector.resources} x $0.001 = ${cost:.3f} "
        "(deduplicated per UTC day, so further runs today are free).\n\n"
        "See [`memory/METRICS.md`](../blob/main/memory/METRICS.md).\n"
    )
    return EXIT_OK


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
