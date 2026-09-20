"""Keeps the three places a setting has to exist in agreement.

A setting lives in bot/config.py, has to be passed through in
.github/workflows/post.yml, and is documented in README.md and .env.example.
Miss the workflow and the setting silently does nothing: an unset repository
variable renders as an empty string, so there is no error anywhere, the value
is just absent and the default applies. That happened to two settings, which
is what this file exists to prevent.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
POST_YML = ROOT / ".github/workflows/post.yml"
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_PY = ROOT / "bot/config.py"

# Read from the environment by other means, not exposed as repo variables.
NOT_REPO_VARIABLES = {"LOG_LEVEL", "GITHUB_EVENT_NAME", "GITHUB_STEP_SUMMARY"}
SECRETS = {
    "ANTHROPIC_API_KEY",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
}


def settings_read_by_config() -> set[str]:
    """Every env var bot/config.py reads through its _env_* helpers."""
    source = CONFIG_PY.read_text(encoding="utf-8")
    return set(re.findall(r'_env_(?:bool|int|str)\(\s*"([A-Z_]+)"', source))


def post_step_env() -> dict:
    workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
    step = next(
        s for s in workflow["jobs"]["post"]["steps"] if s.get("name") == "Write a post"
    )
    return step["env"]


class TestEverySettingIsWired:
    def test_config_reads_something(self):
        assert settings_read_by_config(), "found no settings in bot/config.py"

    @pytest.mark.parametrize("name", sorted(settings_read_by_config() - NOT_REPO_VARIABLES))
    def test_the_workflow_passes_it_through(self, name):
        assert name in post_step_env(), (
            f"{name} is read by bot/config.py but never passed in post.yml, "
            "so setting the repository variable would silently do nothing"
        )

    @pytest.mark.parametrize("name", sorted(settings_read_by_config() - NOT_REPO_VARIABLES))
    def test_it_is_documented(self, name):
        text = README.read_text(encoding="utf-8") + ENV_EXAMPLE.read_text(encoding="utf-8")
        assert name in text, f"{name} is a real setting but is documented nowhere"

    def test_every_secret_is_passed_in(self):
        env = post_step_env()
        for name in SECRETS:
            assert name in env, f"{name} is not passed to the step"
            assert "secrets." in str(env[name]), f"{name} must come from secrets"

    def test_no_secret_is_read_from_a_repository_variable(self):
        # Repository variables are readable by anyone who can see the repo.
        env = post_step_env()
        for name in SECRETS:
            assert "vars." not in str(env[name]), f"{name} must not come from vars"


class TestWorkflowSafety:
    def test_the_model_step_finishes_before_the_job_is_cancelled(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        job = workflow["jobs"]["post"]
        step = next(s for s in job["steps"] if s.get("name") == "Write a post")

        # Otherwise a stalled model call means the always() commit step never
        # runs, the failure is never recorded, and the breaker never counts it.
        assert step["timeout-minutes"] < job["timeout-minutes"]

    def test_the_memory_commit_runs_even_after_a_failure(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        step = next(
            s for s in workflow["jobs"]["post"]["steps"]
            if s.get("name") == "Commit memory"
        )
        assert "always()" in str(step.get("if", ""))

    def test_the_commit_can_actually_push(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        assert workflow["permissions"]["contents"] == "write"

    def test_runs_cannot_overlap(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        concurrency = workflow["concurrency"]
        # cancel-in-progress would kill a run that has already published,
        # losing the memory of it.
        assert concurrency["cancel-in-progress"] is False

    def test_the_schedule_avoids_the_top_of_the_hour(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        for entry in workflow[True]["schedule"]:      # `on:` parses as True
            minute = entry["cron"].split()[0]
            assert minute != "0", (
                "GitHub delays and sometimes drops scheduled runs during the "
                "load spike at the top of each hour"
            )

    def test_the_temp_file_is_never_staged(self):
        # A run killed mid-save leaves state.json.tmp behind; committing it
        # would put it in public history permanently.
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        step = next(
            s for s in workflow["jobs"]["post"]["steps"]
            if s.get("name") == "Commit memory"
        )
        assert "':!memory/*.tmp'" in step["run"]


class TestEffortInput:
    def test_the_dropdown_offers_a_repo_default_sentinel(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        effort = workflow[True]["workflow_dispatch"]["inputs"]["effort"]

        # A choice input always has a value, so without a sentinel
        # `inputs.effort || vars.EFFORT` can never reach the variable.
        assert effort["default"] == "repo-default"
        assert "repo-default" in effort["options"]

    def test_the_expression_falls_through_on_the_sentinel(self):
        expr = str(post_step_env()["EFFORT"])
        assert "repo-default" in expr
        assert "vars.EFFORT" in expr

    def test_every_valid_effort_is_offered(self):
        from bot.config import ConfigError, load_config

        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        options = set(workflow[True]["workflow_dispatch"]["inputs"]["effort"]["options"])
        assert {"low", "medium", "high", "xhigh", "max"} <= options


class TestTheDocsMatchTheCode:
    """Every default in the README's table is a promise. They drift silently."""

    def _defaults_table(self) -> dict:
        text = README.read_text(encoding="utf-8")
        return dict(re.findall(r"\|\s*`([A-Z_]+)`\s*\|\s*`([^`]*)`\s*\|", text))

    def _config(self):
        from bot.config import Config

        return Config(
            anthropic_api_key="x", x_api_key="a", x_api_secret="b",
            x_access_token="c", x_access_token_secret="d",
        )

    @pytest.mark.parametrize(
        "variable,attribute",
        [
            ("DRY_RUN", "dry_run"),
            ("MODEL", "model"),
            ("EFFORT", "effort"),
            ("ENABLE_WEB_SEARCH", "enable_web_search"),
            ("ENABLE_REFUSAL_FALLBACK", "enable_refusal_fallback"),
            ("ALLOW_LINKS", "allow_links"),
            ("MONTHLY_POST_BUDGET", "monthly_post_budget"),
            ("FEED_SAMPLE_SIZE", "feed_sample_size"),
            ("FEED_ITEMS_PER_SOURCE", "feed_items_per_source"),
            ("RECENT_POSTS_IN_CONTEXT", "recent_posts_in_context"),
            ("MAX_CONSECUTIVE_FAILURES", "max_consecutive_failures"),
            ("MAX_SEARCH_ROUNDS", "max_search_rounds"),
        ],
    )
    def test_the_documented_default_is_the_real_default(self, variable, attribute):
        documented = self._defaults_table().get(variable)
        real = getattr(self._config(), attribute)
        expected = str(real).lower() if isinstance(real, bool) else str(real)

        assert documented == expected, (
            f"README says {variable} defaults to {documented!r}, "
            f"but bot/config.py uses {expected!r}"
        )

    def test_the_feed_count_is_right(self):
        from bot.config import FEEDS_PATH
        from bot.explore import load_sources

        actual = len(load_sources(FEEDS_PATH))
        claimed = {int(n) for n in re.findall(r"(\d+) feeds", README.read_text(encoding="utf-8"))}
        assert claimed <= {actual}, f"README claims {claimed} feeds, there are {actual}"

    def test_the_documented_cron_is_the_real_cron(self):
        workflow = yaml.safe_load(POST_YML.read_text(encoding="utf-8"))
        cron = workflow[True]["schedule"][0]["cron"]
        assert cron in README.read_text(encoding="utf-8")

    def test_the_documented_character_limit_is_the_real_one(self):
        from bot.config import POST_CHAR_LIMIT

        claimed = {int(n) for n in re.findall(r"(\d+) weighted characters", README.read_text(encoding="utf-8"))}
        assert claimed <= {POST_CHAR_LIMIT}
