"""Keep the suite out of the real memory/ directory.

bot/config.py holds module-level paths pointing at this checkout. A test that
drives an entry point without redirecting them writes to the committed files
for real, and the failure is invisible: the test passes, and the repository
quietly gains a metrics file or, as actually happened, a state.json carrying
a fake account id that the live bot would then have used.

So every test gets a sandbox by default. Tests that redirect the paths
themselves still work -- their monkeypatch simply wins.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import bot.config as config

REDIRECTED = (
    "MEMORY_DIR",
    "STATE_PATH",
    "POSTS_PATH",
    "FEED_CACHE_PATH",
    "METRICS_PATH",
    "METRICS_MD_PATH",
    "OUT_DIR",
)


@pytest.fixture(autouse=True)
def sandbox_every_path(tmp_path, monkeypatch):
    memory = tmp_path / "sandbox" / "memory"
    memory.mkdir(parents=True)

    for name in REDIRECTED:
        real = getattr(config, name, None)
        if real is None:
            continue
        replacement = (
            memory if name == "MEMORY_DIR"
            else tmp_path / "sandbox" / "out" if name == "OUT_DIR"
            else memory / real.name
        )
        monkeypatch.setattr(config, name, replacement)

    yield


@pytest.fixture(autouse=True)
def no_real_repo_writes():
    """Fail loudly if anything landed in the committed memory/ directory."""
    tracked = {"README.md", "posts.jsonl", "state.json", "feed_cache.json"}
    before = {p.name for p in config.REPO_ROOT.joinpath("memory").glob("*")}

    yield

    after = {p.name for p in config.REPO_ROOT.joinpath("memory").glob("*")}
    created = after - before - tracked
    assert not created, f"a test wrote into the real memory/: {sorted(created)}"
