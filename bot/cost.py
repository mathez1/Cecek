"""What a run actually cost.

Every response carries exact token counts and server-tool call counts, so the
bot can price itself rather than leaving anyone to argue about an estimate.
The numbers land in the Actions run summary and accumulate per month in
memory/state.json.

Prices are per million tokens and are a snapshot, not a live feed. They are
the first thing to go stale here, so they live in one table and can be
corrected without touching anything else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float

    # Standard Anthropic cache multipliers: a write costs 1.25x the base input
    # rate, a read 0.1x. Verify against current docs before trusting a total
    # to the cent; they matter little here because the cacheable prefix is one
    # small persona file.
    @property
    def cache_write(self) -> float:
        return self.input * 1.25

    @property
    def cache_read(self) -> float:
        return self.input * 0.1


PRICES: dict[str, Price] = {
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
    "claude-fable-5-1": Price(10.0, 50.0),
    "claude-fable-5": Price(10.0, 50.0),
}

# $10 per 1,000 searches.
WEB_SEARCH_COST = 0.01

# Web fetch is not priced in this table because I could not confirm a rate.
# Fetches are counted and reported so the omission is visible rather than
# silent; if they are billed, the dollar total here is a floor.
WEB_FETCH_COST = 0.0


@dataclass
class Meter:
    """Accumulates usage across every API call in one run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    web_fetches: int = 0

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        self.calls += 1
        self.input_tokens += _int(getattr(usage, "input_tokens", 0))
        self.output_tokens += _int(getattr(usage, "output_tokens", 0))
        self.cache_read_tokens += _int(getattr(usage, "cache_read_input_tokens", 0))
        self.cache_write_tokens += _int(
            getattr(usage, "cache_creation_input_tokens", 0)
        )

        tools = getattr(usage, "server_tool_use", None)
        if tools is not None:
            self.web_searches += _int(getattr(tools, "web_search_requests", 0))
            self.web_fetches += _int(getattr(tools, "web_fetch_requests", 0))

    def cost(self, model: str) -> float | None:
        """Dollars for this run, or None if the model's price is unknown."""
        price = PRICES.get(model)
        if price is None:
            log.warning("no price on file for %s; not reporting a cost", model)
            return None

        return (
            self.input_tokens / 1e6 * price.input
            + self.output_tokens / 1e6 * price.output
            + self.cache_read_tokens / 1e6 * price.cache_read
            + self.cache_write_tokens / 1e6 * price.cache_write
            + self.web_searches * WEB_SEARCH_COST
            + self.web_fetches * WEB_FETCH_COST
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def report(self, model: str) -> str:
        """A line for the run summary."""
        parts = [
            f"{self.calls} call{'s' if self.calls != 1 else ''}",
            f"{self.input_tokens:,} in",
            f"{self.output_tokens:,} out",
        ]
        if self.cache_read_tokens:
            parts.append(f"{self.cache_read_tokens:,} cached")
        if self.web_searches:
            parts.append(f"{self.web_searches} search{'es' if self.web_searches != 1 else ''}")
        if self.web_fetches:
            parts.append(f"{self.web_fetches} fetch{'es' if self.web_fetches != 1 else ''}")

        spent = self.cost(model)
        if spent is None:
            return ", ".join(parts)

        return f"${spent:.3f} ({', '.join(parts)})"


def project(cost_per_run: float, runs_per_day: int = 24) -> str:
    """What this run's cost annualises to, which is the number that matters."""
    return (
        f"${cost_per_run * runs_per_day:.2f}/day, "
        f"${cost_per_run * runs_per_day * 30:.0f}/month at {runs_per_day} runs a day"
    )


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
