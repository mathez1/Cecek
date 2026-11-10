"""Which hat BoB wears this run: reporter, or off the clock.

The owner's split is about seven reports in ten. Asking the model to keep a
ratio does not work, because every run starts fresh and a model told "seventy
percent" picks reporting every time it is asked. So the choice is made here,
from what was actually posted recently, before the model is called at all.

Deterministic on purpose: given the same history it picks the same mode, so a
test can pin the mix down and a run can be explained after the fact.
"""

from __future__ import annotations

from typing import Iterable

NEWS = "news"
FUN = "fun"
MODES = (NEWS, FUN)

# How far back to look when balancing. Ten posts is about a day at the cap,
# long enough to hold the ratio and short enough to correct a lopsided day.
WINDOW = 10


def pick(history: Iterable, news_share: float, window: int = WINDOW) -> str:
    """Report unless reports are already over their share of recent posts."""
    share = min(max(news_share, 0.0), 1.0)
    if share >= 1.0:
        return NEWS
    if share <= 0.0:
        return FUN

    # The window includes the post about to be made, so look at one fewer.
    recent = [getattr(p, "mode", "") for p in list(history)[-(window - 1):]]
    recent = [m for m in recent if m in MODES]
    if not recent:
        # A fresh account, or history written before modes existed: lead
        # with the thing the account is now for.
        return NEWS

    # Whichever choice leaves the recent mix nearer the target. A plain
    # "report while under the share" settles a few points below it, because
    # it switches away at exactly the target and so averages just under.
    news = recent.count(NEWS)
    total = len(recent) + 1
    if_news = abs((news + 1) / total - share)
    if_fun = abs(news / total - share)
    return NEWS if if_news <= if_fun else FUN
