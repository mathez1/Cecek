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
    anthropic_api_key: str
    x_api_key: str
    x_api_secret: str
    x_access_token: str
    x_access_token_secret: str

    # --- model ---
    model: str = "claude-opus-5"
    effort: str = "high"
    enable_web_search: bool = True
    enable_refusal_fallback: bool = True
    max_search_rounds: int = 6

    # --- behaviour ---
    dry_run: bool = False
    feed_sample_size: int = 12
    feed_items_per_source: int = 6
    recent_posts_in_context: int = 40
    # 0 disables the cap. X's free tier allows 500 posts/month, so hourly
    # posting (~720/month) overruns it; set this to stay inside a plan.
    monthly_post_budget: int = 0

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
        **x_creds,
        model=_env_str("MODEL", "claude-opus-5"),
        effort=_env_str("EFFORT", "high"),
        enable_web_search=_env_bool("ENABLE_WEB_SEARCH", True),
        enable_refusal_fallback=_env_bool("ENABLE_REFUSAL_FALLBACK", True),
        max_search_rounds=_env_int("MAX_SEARCH_ROUNDS", 6),
        dry_run=dry_run,
        feed_sample_size=_env_int("FEED_SAMPLE_SIZE", 12),
        feed_items_per_source=_env_int("FEED_ITEMS_PER_SOURCE", 6),
        recent_posts_in_context=_env_int("RECENT_POSTS_IN_CONTEXT", 40),
        monthly_post_budget=_env_int("MONTHLY_POST_BUDGET", 0),
        allow_links=_env_bool("ALLOW_LINKS", False),
        max_consecutive_failures=_env_int("MAX_CONSECUTIVE_FAILURES", 6),
    )

    valid_efforts = {"low", "medium", "high", "xhigh", "max"}
    if cfg.effort not in valid_efforts:
        raise ConfigError(
            f"EFFORT must be one of {sorted(valid_efforts)}, got {cfg.effort!r}"
        )

    return cfg
