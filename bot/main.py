"""One run = one post.

Invoked by .github/workflows/post.yml on a cron. Everything it needs to know
about previous runs it reads from memory/, which the workflow commits back.

Exit codes matter, because a red cross in the Actions tab is the only alert
this thing has:
  0  posted, dry-ran, or hit something that fixes itself by the next tick
  1  needs a human (bad credentials, exhausted quota, cannot write a good post)
"""

from __future__ import annotations

import logging
import os
import random
import sys
from pathlib import Path

from bot import explore, guard, memory
from bot.brain import Brain, BrainError, Draft, Exploration
from bot.config import POST_CHAR_LIMIT, Config, ConfigError, load_config
from bot.x_client import (
    PostResult,
    XAuthError,
    XClient,
    XDuplicateError,
    XError,
    XQuotaError,
    XRateLimitError,
)

log = logging.getLogger("bot")

EXIT_OK = 0
EXIT_NEEDS_HUMAN = 1


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # These are chatty and never say anything useful at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def summary(markdown: str) -> None:
    """Write to the Actions run summary, so a run is readable without logs."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip() + "\n")
    except OSError as exc:
        log.warning("could not write the step summary: %s", exc)


def load_persona(cfg_path: Path) -> str:
    if not cfg_path.exists():
        log.warning("no persona at %s; falling back to a minimal one", cfg_path)
        return (
            "You run an X account by yourself. Write one specific, interesting "
            "post. No hashtags, no threads, no engagement bait, no em dashes."
        )
    return cfg_path.read_text(encoding="utf-8").strip()


def write_draft(
    brain: Brain,
    exploration: Exploration,
    recent: list[str],
    persona: str,
    cfg: Config,
) -> Draft:
    """Compose, check, and re-compose with feedback until something passes."""
    feedback: str | None = None
    last_error = "unknown"

    for attempt in range(1, cfg.max_compose_attempts + 1):
        log.info("composing (attempt %d of %d)", attempt, cfg.max_compose_attempts)
        draft = brain.compose(exploration, recent, persona, feedback=feedback)

        result = guard.check_draft(
            draft.post,
            recent,
            char_limit=POST_CHAR_LIMIT,
            banned_openers=cfg.banned_openers,
            similarity_threshold=cfg.similarity_threshold,
            allow_links=cfg.allow_links,
        )

        if result.ok and draft.confidence != "low":
            log.info("draft accepted (%d weighted chars)", result.weighted_length)
            return draft

        if not result.ok:
            log.warning("draft rejected: %s", "; ".join(result.problems))
            feedback = result.feedback()
            last_error = "; ".join(result.problems)
        else:
            # The model told us it is unsure of its own facts. Believe it.
            log.warning("draft self-reported low confidence; asking for another")
            feedback = (
                "You marked that draft's confidence as low, which means you were "
                "not sure its factual claims are true. Do not post it. Write "
                "about something you are genuinely confident about instead, or "
                "make a point that does not depend on an unverified fact."
            )
            last_error = "low self-reported confidence"

    raise BrainError(
        f"could not produce a usable post in {cfg.max_compose_attempts} attempts "
        f"(last problem: {last_error})"
    )


def tripped_breaker(state: dict, cfg: Config, manual_run: bool) -> str | None:
    """Refuse to spend anything when the last N runs all failed.

    Without this, a revoked X token or an empty credit balance quietly turns
    into a full month of Claude calls that can never produce a post.
    """
    if manual_run or cfg.max_consecutive_failures <= 0:
        return None

    failures = int(state.get("consecutive_failures", 0))
    if failures < cfg.max_consecutive_failures:
        return None

    return (
        f"The last {failures} runs failed in a row, so this one stopped before "
        "calling Claude."
    )


def run() -> int:
    setup_logging()

    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        summary(f"## Run failed\n\nConfiguration problem:\n\n> {exc}\n")
        return EXIT_NEEDS_HUMAN

    log.info(
        "starting: model=%s effort=%s web_search=%s dry_run=%s",
        cfg.model,
        cfg.effort,
        cfg.enable_web_search,
        cfg.dry_run,
    )

    from bot.config import (
        FEED_CACHE_PATH,
        FEEDS_PATH,
        PERSONA_PATH,
        POSTS_PATH,
        STATE_PATH,
    )

    state = memory.load_state(STATE_PATH)
    memory.touch_run(state)

    # A run started by hand is a deliberate "try again", so it always gets
    # through the breaker and clears the streak.
    manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if manual_run and state.get("consecutive_failures"):
        log.info("manual run: clearing a streak of %d failures", state["consecutive_failures"])
        state["consecutive_failures"] = 0

    breaker = tripped_breaker(state, cfg, manual_run)
    if breaker:
        log.error("%s", breaker)
        memory.save_state(STATE_PATH, state)
        summary(
            f"## Stopped, needs you\n\n{breaker}\n\n"
            "Nothing was sent to Claude, so this run cost nothing. Fix the "
            "cause, then use **Run workflow** to clear the streak and resume. "
            "The last error was:\n\n"
            f"> {state.get('last_status')}\n"
        )
        return EXIT_NEEDS_HUMAN
    history = memory.load_posts(POSTS_PATH, limit=cfg.recent_posts_in_context)
    recent = memory.recent_texts(history)
    log.info("memory: %d recent posts in context", len(recent))

    # Stop before spending anything if a self-imposed monthly cap is reached.
    if cfg.monthly_post_budget > 0:
        used = memory.posts_this_month(state)
        if used >= cfg.monthly_post_budget:
            reason = f"monthly budget reached ({used}/{cfg.monthly_post_budget})"
            log.warning("%s; not posting", reason)
            memory.record_skip(state, reason)
            memory.save_state(STATE_PATH, state)
            summary(f"## Skipped\n\n{reason}. Posting resumes next month.\n")
            return EXIT_OK

    persona = load_persona(PERSONA_PATH)

    sources = explore.load_sources(FEEDS_PATH)
    feed_cache = explore.FeedCache(FEED_CACHE_PATH)
    items = explore.gather(
        sources,
        sample_size=cfg.feed_sample_size,
        items_per_source=cfg.feed_items_per_source,
        rng=random.Random(),
        cache=feed_cache,
    )
    feed_cache.prune({s.url for s in sources})
    feed_cache.save()
    digest = explore.build_digest(items)
    log.info("digest: %d items from %d feeds", len(items), len(sources))

    brain = Brain(cfg)

    try:
        exploration = brain.explore(digest, recent, persona)
        draft = write_draft(brain, exploration, recent, persona, cfg)
    except BrainError as exc:
        log.error("could not write a post: %s", exc)
        memory.record_failure(state, str(exc))
        memory.save_state(STATE_PATH, state)
        summary(f"## Run failed\n\nCould not write a post:\n\n> {exc}\n")
        return EXIT_NEEDS_HUMAN

    log.info("draft: %s", draft.post)

    if cfg.dry_run:
        log.info("DRY_RUN is on; not posting")
        memory.record_skip(state, "dry run")
        memory.save_state(STATE_PATH, state)
        summary(
            "## Dry run\n\nNothing was posted. The draft was:\n\n"
            f"> {draft.post}\n\n"
            f"**Topic:** {draft.topic}  \n"
            f"**Why:** {draft.rationale}  \n"
            f"**Confidence:** {draft.confidence}  \n"
            f"**Length:** {guard.weighted_length(draft.post)}/{POST_CHAR_LIMIT}\n"
        )
        return EXIT_OK

    client = XClient(cfg)
    result = publish(client, brain, draft, exploration, recent, persona, cfg, state)

    if result is None:
        memory.save_state(STATE_PATH, state)
        status = str(state.get("last_status", ""))
        return EXIT_NEEDS_HUMAN if status.startswith("failed") else EXIT_OK

    memory.append_post(
        POSTS_PATH,
        memory.PostRecord(
            text=result.text,
            topic=draft.topic,
            rationale=draft.rationale,
            sources=draft.sources or exploration.sources[:5],
            tweet_id=result.tweet_id,
            url=result.url,
        ),
    )
    memory.record_success(state)
    memory.save_state(STATE_PATH, state)

    log.info("done: %s", result.url)
    summary(
        f"## Posted\n\n> {result.text}\n\n"
        f"[View on X]({result.url})\n\n"
        f"**Topic:** {draft.topic}  \n"
        f"**Why:** {draft.rationale}  \n"
        f"**Confidence:** {draft.confidence}  \n"
        f"**Length:** {guard.weighted_length(result.text)}/{POST_CHAR_LIMIT}\n"
    )
    return EXIT_OK


def publish(
    client: XClient,
    brain: Brain,
    draft: Draft,
    exploration: Exploration,
    recent: list[str],
    persona: str,
    cfg: Config,
    state: dict,
) -> PostResult | None:
    """Post it. Returns None if the run should end without a published post."""
    try:
        return client.post(draft.post)

    except XDuplicateError as exc:
        # X's duplicate detection is fuzzy and undocumented, so it can reject
        # something our own similarity check passed. Worth exactly one retry.
        log.warning("%s; rewriting once", exc)
        try:
            retry = brain.compose(
                exploration,
                recent,
                persona,
                feedback=(
                    "X rejected that post as duplicate content. Its matching is "
                    "fuzzy, so a light rewording will be rejected too. Write "
                    "about a different angle entirely.\n\n"
                    f"Rejected draft:\n  {draft.post}"
                ),
            )
            check = guard.check_draft(
                retry.post,
                recent,
                char_limit=POST_CHAR_LIMIT,
                banned_openers=cfg.banned_openers,
                similarity_threshold=cfg.similarity_threshold,
                allow_links=cfg.allow_links,
            )
            if not check.ok:
                raise BrainError("; ".join(check.problems))
            draft.post = retry.post
            draft.topic = retry.topic
            draft.rationale = retry.rationale
            return client.post(retry.post)
        except (BrainError, XError) as retry_exc:
            log.error("the rewrite failed too: %s", retry_exc)
            memory.record_failure(state, f"duplicate, rewrite failed: {retry_exc}")
            summary(f"## Run failed\n\nX rejected the post as duplicate content, "
                    f"and the rewrite also failed:\n\n> {retry_exc}\n")
            return None

    except XRateLimitError as exc:
        # Self-healing. The next hourly tick will try again; a red run here
        # would train everyone to ignore red runs.
        log.warning("%s", exc)
        memory.record_skip(state, "rate limited by X")
        summary(
            f"## Skipped\n\nX rate limited this run:\n\n> {exc}\n\n"
            "This clears on its own. The next scheduled run will try again.\n"
        )
        return None

    except XQuotaError as exc:
        log.error("%s", exc)
        memory.record_failure(state, "X usage cap exceeded")
        summary(
            f"## Run failed, needs you\n\n> {exc}\n\n"
            "Add credit in the X developer console, or lower the posting "
            "frequency in `.github/workflows/post.yml`.\n"
        )
        return None

    except XAuthError as exc:
        log.error("%s", exc)
        memory.record_failure(state, "X rejected the credentials")
        summary(f"## Run failed, needs you\n\n> {exc}\n")
        return None

    except XError as exc:
        log.error("posting failed: %s", exc)
        memory.record_failure(state, str(exc))
        summary(f"## Run failed\n\n> {exc}\n")
        return None


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
