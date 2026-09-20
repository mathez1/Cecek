#!/usr/bin/env python3
"""Report which feeds in prompts/feeds.txt are dead.

Feeds rot quietly: a publisher redesigns, the URL 404s, and the bot just gets a
slightly narrower view of the world without ever failing. Run this occasionally
(the `feeds` workflow does it monthly) and prune what it reports.

    python tools/check_feeds.py
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser  # noqa: E402

from bot.config import FEEDS_PATH  # noqa: E402
from bot.explore import USER_AGENT, Source, load_sources  # noqa: E402


def check(source: Source) -> tuple[Source, bool, str]:
    try:
        parsed = feedparser.parse(source.url, agent=USER_AGENT)
    except Exception as exc:
        return source, False, f"error: {exc}"

    status = getattr(parsed, "status", None)
    entries = getattr(parsed, "entries", []) or []

    if status is not None and status >= 400:
        return source, False, f"HTTP {status}"
    if not entries:
        note = getattr(parsed, "bozo_exception", "") or "no entries"
        return source, False, str(note)[:70]

    dated = sum(
        1
        for e in entries
        if e.get("published_parsed") or e.get("updated_parsed")
    )
    return source, True, f"{len(entries)} entries, {dated} dated"


def main() -> int:
    sources = load_sources(FEEDS_PATH)
    if not sources:
        print(f"No feeds found in {FEEDS_PATH}")
        return 1

    print(f"Checking {len(sources)} feeds from {FEEDS_PATH}\n")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(check, sources))

    working = [r for r in results if r[1]]
    broken = [r for r in results if not r[1]]

    for source, _, note in sorted(working, key=lambda r: r[0].name):
        print(f"  ok    {source.name:24} {note}")

    if broken:
        print()
        for source, _, note in sorted(broken, key=lambda r: r[0].name):
            print(f"  DEAD  {source.name:24} {note}\n        {source.url}")

    print(f"\n{len(working)} working, {len(broken)} dead.")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"## Feeds\n\n{len(working)} working, {len(broken)} dead.\n\n")
            if broken:
                handle.write("| Feed | Problem | URL |\n|---|---|---|\n")
                for source, _, note in sorted(broken, key=lambda r: r[0].name):
                    handle.write(f"| {source.name} | {note} | {source.url} |\n")

    # A few dead feeds are normal and harmless; the bot skips them. Only fail
    # if the list has decayed far enough to actually narrow what it reads.
    if len(working) < max(8, len(sources) // 2):
        print("\nToo few feeds are working. Prune and replace some.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
