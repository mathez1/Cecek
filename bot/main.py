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

from bot import cost, explore, guard, imagegen, memory, visual
from bot.brain import Brain, BrainError, Draft, Exploration
from bot.config import POST_CHAR_LIMIT, Config, ConfigError, load_config
from bot.media import MediaError, MediaUploader
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
    history: list,
    recent: list[str],
    persona: str,
    cfg: Config,
) -> Draft:
    """Compose, check, and re-compose with feedback until something passes."""
    feedback: str | None = None
    last_error = "unknown"

    for attempt in range(1, cfg.max_compose_attempts + 1):
        log.info("composing (attempt %d of %d)", attempt, cfg.max_compose_attempts)

        try:
            draft = brain.compose(exploration, history, persona, feedback=feedback)
        except BrainError as exc:
            # A truncated or unparseable response is worth another go; we
            # already paid for the exploration that produced these notes.
            if attempt == cfg.max_compose_attempts:
                raise
            log.warning("composition failed (%s); retrying", exc)
            last_error = str(exc)
            continue

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


def draw(
    draft: Draft, cfg: Config, state: dict, out_dir: Path
) -> tuple[object | None, object | None]:
    """Make the picture the model asked for, if it asked for one.

    Returns the rendered picture and the illustrator that was used, the
    latter only so the run can report what it spent. Both are None when the
    post goes out as text, which is the common case and not a failure.
    """
    kind = getattr(draft.visual, "kind", "none")
    if not cfg.enable_visuals or kind == "none":
        return None, None

    illustrator = None
    if kind == "illustration":
        # The only branch that can spend money, so it is the only one that
        # looks at a key or a budget. Charts are drawn here regardless.
        if not cfg.image_api_key:
            log.info("an illustration was chosen but IMAGE_API_KEY is not set")
            return None, None

        used = memory.images_today(state)
        if 0 < cfg.daily_image_budget <= used:
            log.info(
                "daily image budget reached (%d/%d); posting without a picture",
                used,
                cfg.daily_image_budget,
            )
            return None, None

        illustrator = imagegen.Illustrator(cfg)

    rendered = visual.render(draft.visual, out_dir, illustrator)

    # Counted from what the vendor was actually asked for, not from whether a
    # picture came out of it. An image that was paid for and then discarded -
    # too large, or unmarkable - still spent money, and billing it to nothing
    # let a one-a-day cap pay for every run of the day.
    for _ in range(getattr(illustrator, "images", 0)):
        memory.record_image(state)

    if rendered is None:
        return None, illustrator

    log.info("drew a %s: %s", rendered.kind, rendered.path)
    return rendered, illustrator


def _picture_note(rendered: object | None, draft: Draft, attached: bool = True) -> str:
    """The summary line about the picture, which is often 'there isn't one'.

    Three outcomes are worth telling apart: no picture was wanted, one was
    wanted and could not be drawn, and one was drawn and then rejected by the
    upload. Collapsing the last two would hide which half is broken.
    """
    wanted = getattr(draft.visual, "kind", "none")

    if rendered is None:
        if wanted == "none":
            return "**Picture:** none, by choice  \n"
        return f"**Picture:** a {wanted} was wanted but could not be drawn  \n"

    if not attached:
        return f"**Picture:** a {rendered.kind} was drawn but X would not take it  \n"

    return f"**Picture:** {rendered.kind}  \n**Alt text:** {rendered.alt}  \n"


def _media_note(illustrator: object | None, uploader: object | None, cfg: Config) -> str:
    """What the picture cost, including the part nobody can price yet.

    X publishes a rate for creating a post and one for alt-text metadata, and
    nothing at all for the upload endpoints. Rather than quietly assume they
    are free, every call is counted here so the figure can be checked against
    the developer console after a few runs.
    """
    images = getattr(illustrator, "images", 0)
    calls = getattr(uploader, "calls", 0)
    if not images and not calls:
        return ""

    parts = []
    if images:
        parts.append(f"{images} generated image ({cfg.image_model})")
    if calls:
        parts.append(f"{calls} X media call{'s' if calls != 1 else ''}, price unpublished")
    return "**Picture cost:** " + ", ".join(parts) + "  \n"


def _spend_note(meter: cost.Meter, state: dict, cfg: Config) -> str:
    """The line that turns one run's price into the number that matters."""
    spent = meter.cost(cfg.model)
    if spent is None:
        return ""

    memory.record_spend(state, spent)

    runs_per_day = cfg.daily_post_budget or 24
    note = f"\nAt this rate: {cost.project(spent, runs_per_day)}.\n"

    month = memory.spend_this_month(state)
    if month:
        note += f"\nModel spend so far this month: ${month:.2f}.\n"
    return note


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


CONFIG_FAILURE = "configuration"


def run() -> int:
    setup_logging()

    from bot.config import (
        FEED_CACHE_PATH,
        FEEDS_PATH,
        OUT_DIR,
        PERSONA_PATH,
        POSTS_PATH,
        STATE_PATH,
    )

    # State is loaded BEFORE the config, because a broken config is itself
    # worth recording. Returning early without writing it meant the breaker
    # could never see the single most likely persistent failure, and a repo
    # with no secrets set logged a red run every hour forever instead of six.
    # Nothing here depends on the config: these paths are module constants.
    state = memory.load_state(STATE_PATH)
    memory.touch_run(state)

    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        memory.record_failure(state, f"{CONFIG_FAILURE}: {exc}")
        memory.save_state(STATE_PATH, state)
        summary(
            f"## Run failed\n\nConfiguration problem:\n\n> {exc}\n\n"
            "Nothing was sent to Claude, so this run cost nothing. Once the "
            "setting is in place the next scheduled run picks it up by "
            "itself.\n"
        )
        return EXIT_NEEDS_HUMAN

    # The config loaded, so whatever was missing is now present. A streak of
    # configuration failures is therefore demonstrably over, and clearing it
    # here is what stops the breaker blocking the first good run after someone
    # finally adds the secrets.
    if str(state.get("last_status", "")).startswith(f"failed: {CONFIG_FAILURE}"):
        log.info(
            "configuration is fixed; clearing a streak of %s failed runs",
            state.get("consecutive_failures"),
        )
        state["consecutive_failures"] = 0
        state["last_status"] = "recovered: configuration fixed"

    log.info(
        "starting: model=%s effort=%s web_search=%s dry_run=%s",
        cfg.model,
        cfg.effort,
        cfg.enable_web_search,
        cfg.dry_run,
    )

    # A run started by hand is a deliberate "try again", so it gets through
    # the breaker. It does NOT clear the streak by itself: the default manual
    # run is a dry run, and a dry run proves nothing about the credentials
    # that caused the streak. Only a published post clears it, via
    # record_success.
    manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    breaker = tripped_breaker(state, cfg, manual_run)
    if breaker:
        log.error("%s", breaker)
        memory.save_state(STATE_PATH, state)
        summary(
            f"## Stopped, needs you\n\n{breaker}\n\n"
            "Nothing was sent to Claude, so this run cost nothing. Fix the "
            "cause, then use **Run workflow** with the dry-run box **unticked** "
            "to resume: a real published post is what clears the streak, and a "
            "dry run cannot prove the credentials work. The last error was:\n\n"
            f"> {state.get('last_status')}\n"
        )
        return EXIT_NEEDS_HUMAN
    history = memory.load_posts(POSTS_PATH, limit=cfg.recent_posts_in_context)
    recent = memory.recent_texts(history)  # texts only, for the similarity guard
    log.info("memory: %d recent posts in context", len(recent))

    # Stop before spending anything if a self-imposed cap is reached. Both
    # checks happen BEFORE the model call, because the model call is the
    # expensive half and a capped run must not pay for a post it cannot make.
    budgets = (
        ("daily", cfg.daily_post_budget, memory.posts_today(state), "tomorrow"),
        ("monthly", cfg.monthly_post_budget, memory.posts_this_month(state), "next month"),
    )
    for label, cap, used, resumes in budgets:
        if cap > 0 and used >= cap:
            reason = f"{label} budget reached ({used}/{cap})"
            log.warning("%s; not posting", reason)
            memory.record_skip(state, reason)
            memory.save_state(STATE_PATH, state)
            summary(
                f"## Skipped\n\n{reason}. Nothing was sent to Claude, so this "
                f"run cost nothing. Posting resumes {resumes}.\n"
            )
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
        exploration = brain.explore(digest, history, persona)
        draft = write_draft(brain, exploration, history, recent, persona, cfg)
    except BrainError as exc:
        log.error("could not write a post: %s", exc)
        memory.record_spend(state, brain.meter.cost(cfg.model))
        memory.record_failure(state, str(exc))
        memory.save_state(STATE_PATH, state)
        summary(f"## Run failed\n\nCould not write a post:\n\n> {exc}\n")
        return EXIT_NEEDS_HUMAN

    log.info("draft: %s", draft.post)

    # Drawn before the dry-run branch on purpose: a dry run that skipped the
    # picture could not be used to check the picture, which is most of what
    # anyone would dry-run this for.
    # draw() guards its own internals, but the belt-and-braces handler is
    # here because this is the boundary the governing rule is stated at: a
    # picture is a bonus and may never cost a post. An OSError from a full or
    # read-only disk would otherwise end the run with the post already
    # written and paid for.
    try:
        rendered, illustrator = draw(draft, cfg, state, OUT_DIR)
    except Exception as exc:  # noqa: BLE001
        log.warning("the picture failed outright (%s); posting as text", exc)
        rendered, illustrator = None, None

    if cfg.dry_run:
        log.info("DRY_RUN is on; not posting")
        memory.record_skip(state, "dry run")
        spend_note = _spend_note(brain.meter, state, cfg)
        memory.save_state(STATE_PATH, state)
        log.info("this run used %s", brain.meter.report(cfg.model))
        summary(
            "## Dry run\n\nNothing was posted. The draft was:\n\n"
            f"> {draft.post}\n\n"
            f"**Topic:** {draft.topic}  \n"
            f"**Why:** {draft.rationale}  \n"
            f"**Confidence:** {draft.confidence}  \n"
            f"**Length:** {guard.weighted_length(draft.post)}/{POST_CHAR_LIMIT}  \n"
            + _picture_note(rendered, draft)
            + f"**Cost:** {brain.meter.report(cfg.model)}\n"
            + spend_note
        )
        return EXIT_OK

    # A list, not None, and the same object publish() is given: publish
    # empties it in place when it drops the picture, which is how the summary
    # below knows whether the post that went out actually carried one.
    media_ids: list[str] = []
    uploader = None
    if rendered is not None:
        uploader = MediaUploader(cfg)
        try:
            media_ids.append(uploader.upload(rendered.path, rendered.alt))
        except MediaError as exc:
            # The picture was a bonus. Losing it is not worth losing the post.
            log.warning("posting without the picture: %s", exc)

    client = XClient(cfg)
    result = publish(
        client, brain, draft, exploration, history, recent, persona, cfg, state,
        media_ids=media_ids,
    )

    if result is None:
        memory.save_state(STATE_PATH, state)
        status = str(state.get("last_status", ""))
        return EXIT_NEEDS_HUMAN if status.startswith("failed") else EXIT_OK

    # The post is already live at this point, so a disk failure here must not
    # lose it silently: record what we can, and put the text in the summary so
    # a human can restore it by hand.
    try:
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
    except OSError as exc:
        log.error("posted %s but could not write it to memory: %s", result.url, exc)
        summary(
            f"## Posted, but not recorded\n\nThe post is live at {result.url} "
            f"but `memory/posts.jsonl` could not be written:\n\n> {exc}\n\n"
            "The bot does not know it said this, so it may repeat itself. Add "
            "the line by hand:\n\n```\n"
            + memory.PostRecord(
                text=result.text,
                topic=draft.topic,
                rationale=draft.rationale,
                tweet_id=result.tweet_id,
                url=result.url,
            ).to_json()
            + "\n```\n"
        )

    memory.record_success(state)
    # _spend_note mutates state, so it has to run before the save or the
    # monthly total never reaches disk.
    spend_note = _spend_note(brain.meter, state, cfg)
    try:
        memory.save_state(STATE_PATH, state)
    except OSError as exc:
        log.error("could not save state after posting: %s", exc)

    log.info("done: %s", result.url)
    log.info("this run used %s", brain.meter.report(cfg.model))
    summary(
        f"## Posted\n\n> {result.text}\n\n"
        f"[View on X]({result.url})\n\n"
        f"**Topic:** {draft.topic}  \n"
        f"**Why:** {draft.rationale}  \n"
        f"**Confidence:** {draft.confidence}  \n"
        f"**Length:** {guard.weighted_length(result.text)}/{POST_CHAR_LIMIT}  \n"
        + _picture_note(rendered, draft, attached=bool(media_ids))
        + f"**Cost:** {brain.meter.report(cfg.model)}\n"
        + _media_note(illustrator, uploader, cfg)
        + spend_note
    )
    return EXIT_OK


def publish(
    client: XClient,
    brain: Brain,
    draft: Draft,
    exploration: Exploration,
    history: list,
    recent: list[str],
    persona: str,
    cfg: Config,
    state: dict,
    media_ids: list[str] | None = None,
) -> PostResult | None:
    """Post it. Returns None if the run should end without a published post.

    The duplicate path loops back into the same try rather than posting from
    inside the except clause, because an error raised in a handler cannot be
    caught by that handler's siblings: a rate limit hit while retrying a
    duplicate would otherwise be recorded as a hard failure and go red, when
    it is the one X error that fixes itself.
    """
    rewrites_left = 1

    while True:
        try:
            return client.post(draft.post, media_ids=media_ids or None)

        except XDuplicateError as exc:
            # X's duplicate detection is fuzzy and undocumented, so it can
            # reject something our own similarity check passed.
            if rewrites_left <= 0:
                log.error("still duplicate after a rewrite: %s", exc)
                memory.record_failure(state, "duplicate after a rewrite")
                summary(
                    "## Run failed\n\nX rejected both the post and its rewrite "
                    f"as duplicate content:\n\n> {exc}\n"
                )
                return None

            rewrites_left -= 1
            log.warning("%s; rewriting once", exc)

            # The picture was drawn for the angle X just rejected, and the
            # rewrite is required to take a different one. Carrying it over
            # would attach a chart of a claim the new post no longer makes.
            # Cleared in place so the caller's summary knows it went.
            if media_ids:
                log.info("dropping the picture; it belongs to the rejected draft")
                media_ids.clear()

            try:
                retry = brain.compose(
                    exploration,
                    history,
                    persona,
                    feedback=(
                        "X rejected that post as duplicate content. Its matching "
                        "is fuzzy, so a light rewording will be rejected too. "
                        "Write about a different angle entirely.\n\n"
                        f"Rejected draft:\n  {draft.post}"
                    ),
                )
            except BrainError as brain_exc:
                log.error("could not rewrite the duplicate: %s", brain_exc)
                memory.record_failure(state, f"duplicate, rewrite failed: {brain_exc}")
                summary(
                    "## Run failed\n\nX rejected the post as duplicate content "
                    f"and the rewrite failed:\n\n> {brain_exc}\n"
                )
                return None

            check = guard.check_draft(
                retry.post,
                recent,
                char_limit=POST_CHAR_LIMIT,
                banned_openers=cfg.banned_openers,
                similarity_threshold=cfg.similarity_threshold,
                allow_links=cfg.allow_links,
            )
            # The rewrite is held to exactly the same bar as the original,
            # low-confidence check included.
            if not check.ok or retry.confidence == "low":
                reason = "; ".join(check.problems) or "low self-reported confidence"
                log.error("the rewrite did not pass the guards: %s", reason)
                memory.record_failure(state, f"duplicate, rewrite rejected: {reason}")
                summary(
                    "## Run failed\n\nX rejected the post as duplicate content, "
                    f"and the rewrite did not pass the guards:\n\n> {reason}\n"
                )
                return None

            # Carry every field across, not just the text, or posts.jsonl and
            # the run summary describe the draft that was thrown away.
            draft.post = retry.post
            draft.topic = retry.topic
            draft.rationale = retry.rationale
            draft.sources = retry.sources
            draft.confidence = retry.confidence
            continue

        except XRateLimitError as exc:
            # Self-healing. The next hourly tick will try again; a red run here
            # would train everyone to ignore red runs.
            log.warning("%s", exc)
            # Green, because it clears on its own and a red run here would
            # train everyone to ignore red runs. But it still counts toward
            # the breaker: we paid for a post we could not publish, and six
            # of those in a row means something is genuinely stuck.
            memory.record_skip(state, "rate limited by X", counts_as_failure=True)
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
            # An unusable media id fails the whole create_tweet call, so the
            # text goes down with the picture unless it is retried without.
            # This sits in the catch-all rather than its own clause because
            # every specific error above is an XError subclass and a clause
            # ahead of them would shadow all four; and because a rate limit
            # or a quota stop is not about the picture anyway. Clearing the
            # list is what ends the retry: the second time round there is no
            # picture, so this falls through to the failure below.
            if media_ids:
                log.warning(
                    "X refused the post with its picture (%s); retrying as text", exc
                )
                media_ids.clear()
                continue

            log.error("posting failed: %s", exc)
            memory.record_failure(state, str(exc))
            summary(f"## Run failed\n\n> {exc}\n")
            return None


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
