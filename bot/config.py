"""Configuration, loaded from environment variables.

Every knob has a default that works, so a fresh clone only needs the five
secrets. Everything else is optional tuning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROMPTS_DIR = REPO_ROOT / "prompts"
MEMORY_DIR = REPO_ROOT / "memory"

PERSONA_PATH = PROMPTS_DIR / "persona.md"
FEEDS_PATH = PROMPTS_DIR / "feeds.txt"
POSTS_PATH = MEMORY_DIR / "posts.jsonl"
STATE_PATH = MEMORY_DIR / "state.json"

# Rendered pictures. Deliberately outside memory/, which is committed on every
# run: a PNG per post would bloat the repository forever, and the picture is
# already on X once the post lands. The workflow uploads it as a run artifact
# instead, which is how you see what a dry run drew.
OUT_DIR = REPO_ROOT / "out"

# What the tracker writes: the numbers, and the table a person reads.
METRICS_PATH = MEMORY_DIR / "metrics.json"
METRICS_MD_PATH = MEMORY_DIR / "METRICS.md"
FEED_CACHE_PATH = MEMORY_DIR / "feed_cache.json"

# X counts a standard post in weighted characters. 280 is the cap.
POST_CHAR_LIMIT = 280


class ConfigError(RuntimeError):
    """Raised when the environment is missing something the bot cannot run without."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass(frozen=True)
class Config:
    # --- credentials ---
    # repr=False on every one of these. state.json records failure text and is
    # committed to a public repository, where GitHub's log masking does not
    # apply and history is permanent. A Config that reached an exception
    # message would otherwise publish all five secrets.
    anthropic_api_key: str = field(repr=False)
    x_api_key: str = field(repr=False)
    x_api_secret: str = field(repr=False)
    x_access_token: str = field(repr=False)
    x_access_token_secret: str = field(repr=False)
    # Optional, and the only key that is not required to run. Without it the
    # bot draws charts and never generates an illustration.
    image_api_key: str = field(repr=False, default="")

    # --- model ---
    model: str = "claude-opus-5"
    effort: str = "high"
    enable_web_search: bool = True
    enable_refusal_fallback: bool = True
    max_search_rounds: int = 6
    # The main lever on what a run costs. Search results stay in context and
    # are re-read on every later turn of the server-side loop, so the tenth
    # search costs far more than the first.
    max_searches: int = 6
    max_fetches: int = 3

    # --- behaviour ---
    dry_run: bool = False
    feed_sample_size: int = 12
    feed_items_per_source: int = 6
    recent_posts_in_context: int = 40
    # 0 disables the cap. X's free tier allows 500 posts/month, so hourly
    # posting (~720/month) overruns it; set this to stay inside a plan.
    monthly_post_budget: int = 0
    # The cap that actually governs day to day. GitHub delivers scheduled runs
    # unpredictably, so capping posts is more reliable than tuning the cron:
    # schedule generously, let the budget decide how many land.
    daily_post_budget: int = 10

    # --- pictures ---
    # Charts are free: they are drawn locally from numbers the post already
    # states. Illustrations are not, which is why they have their own cap.
    enable_visuals: bool = True
    # The model id is the first thing here to go stale. This one is the id
    # Google's own cookbook calls as of October 2026; a "fast" variant that
    # was assumed earlier appears nowhere in it, and an id the API does not
    # know fails every illustration quietly into a title card.
    image_model: str = "imagen-4.0-generate-001"
    # Matches the daily post cap, because the owner wants an illustration on
    # every post. At about $0.04 each that is up to ~$12 a month. Lower it to
    # spend less: posts past the cap get a chart or a title card instead.
    daily_image_budget: int = 10

    # --- views ---
    # X bills reads per post returned and deduplicates per UTC day, so how
    # OFTEN this runs is free and how FAR BACK it looks is not. Posts older
    # than the window keep their last reading rather than being refreshed:
    # engagement is front-loaded, so a week-old number is close to final.
    metrics_window_days: int = 7
    # The account's numeric id. Resolving it costs a read that is not an
    # Owned Read, so it is looked up once and then cached in state.json.
    x_user_id: str = ""

    # --- guards ---
    similarity_threshold: float = 0.72
    max_compose_attempts: int = 3
    # After this many failed runs in a row, stop calling Claude. A revoked X
    # token would otherwise burn a full month of model spend on posts that can
    # never be published. 0 disables the breaker.
    max_consecutive_failures: int = 6
    # X bills a post containing a URL at a far higher per-post rate than a
    # plain one, so links are off unless you opt in.
    allow_links: bool = False

    banned_openers: tuple[str, ...] = field(
        default_factory=lambda: (
            "ever wondered",
            "here's the thing",
            "let's talk about",
            "most people don't know",
            "most people dont know",
            "nobody talks about",
            "this will blow your mind",
            "let that sink in",
            "the craziest part",
            "hot take",
            "unpopular opinion",
            "thread:",
            "a thread",
            "buckle up",
            "i'll say it",
            "plot twist",
        )
    )

    @property
    def has_x_credentials(self) -> bool:
        return all(
            (
                self.x_api_key,
                self.x_api_secret,
                self.x_access_token,
                self.x_access_token_secret,
            )
        )


def load_config() -> Config:
    """Build a Config from the environment, failing loudly on missing secrets."""
    dry_run = _env_bool("DRY_RUN", False)

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Add it as a repository secret "
            "(Settings > Secrets and variables > Actions)."
        )

    image_api_key = os.environ.get("IMAGE_API_KEY", "").strip()

    x_creds = {
        "x_api_key": os.environ.get("X_API_KEY", "").strip(),
        "x_api_secret": os.environ.get("X_API_SECRET", "").strip(),
        "x_access_token": os.environ.get("X_ACCESS_TOKEN", "").strip(),
        "x_access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip(),
    }

    missing = [name.upper() for name, value in x_creds.items() if not value]
    if missing and not dry_run:
        raise ConfigError(
            "Missing X credentials: "
            + ", ".join(missing)
            + ". Add them as repository secrets, or set DRY_RUN=true to run "
            "without posting."
        )

    cfg = Config(
        anthropic_api_key=anthropic_api_key,
        image_api_key=image_api_key,
        **x_creds,
        model=_env_str("MODEL", "claude-opus-5"),
        effort=_env_str("EFFORT", "high"),
        enable_web_search=_env_bool("ENABLE_WEB_SEARCH", True),
        enable_refusal_fallback=_env_bool("ENABLE_REFUSAL_FALLBACK", True),
        max_search_rounds=_env_int("MAX_SEARCH_ROUNDS", 6),
        max_searches=_env_int("MAX_SEARCHES", 6),
        max_fetches=_env_int("MAX_FETCHES", 3),
        dry_run=dry_run,
        feed_sample_size=_env_int("FEED_SAMPLE_SIZE", 12),
        feed_items_per_source=_env_int("FEED_ITEMS_PER_SOURCE", 6),
        recent_posts_in_context=_env_int("RECENT_POSTS_IN_CONTEXT", 40),
        monthly_post_budget=_env_int("MONTHLY_POST_BUDGET", 0),
        daily_post_budget=_env_int("DAILY_POST_BUDGET", 10),
        allow_links=_env_bool("ALLOW_LINKS", False),
        max_consecutive_failures=_env_int("MAX_CONSECUTIVE_FAILURES", 6),
        enable_visuals=_env_bool("ENABLE_VISUALS", True),
        image_model=_env_str("IMAGE_MODEL", "imagen-4.0-generate-001"),
        daily_image_budget=_env_int("DAILY_IMAGE_BUDGET", 10),
        metrics_window_days=_env_int("METRICS_WINDOW_DAYS", 7),
        x_user_id=_env_str("X_USER_ID", ""),
    )

    valid_efforts = {"low", "medium", "high", "xhigh", "max"}
    if cfg.effort not in valid_efforts:
        raise ConfigError(
            f"EFFORT must be one of {sorted(valid_efforts)}, got {cfg.effort!r}"
        )

    return cfg
