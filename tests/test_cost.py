"""Tests for run cost accounting.

The point of this module is that nobody has to argue about an estimate, so the
arithmetic itself has to be right and it has to degrade quietly when the API
reports something unexpected.
"""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from bot.cost import PRICES, WEB_SEARCH_COST, Meter, project


def usage(**kwargs):
    defaults = dict(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=None,
    )
    defaults.update(kwargs)
    return NS(usage=NS(**defaults))


class TestAccumulation:
    def test_adds_across_calls(self):
        m = Meter()
        m.add(usage(input_tokens=1000, output_tokens=100))
        m.add(usage(input_tokens=2000, output_tokens=200))

        assert m.calls == 2
        assert m.input_tokens == 3000
        assert m.output_tokens == 300

    def test_counts_server_tool_calls(self):
        m = Meter()
        m.add(usage(server_tool_use=NS(web_search_requests=3, web_fetch_requests=2)))
        assert m.web_searches == 3
        assert m.web_fetches == 2

    def test_a_response_without_usage_is_ignored(self):
        m = Meter()
        m.add(NS())
        assert m.calls == 0

    def test_missing_fields_do_not_raise(self):
        m = Meter()
        m.add(NS(usage=NS(input_tokens=None, output_tokens="oops")))
        assert m.input_tokens == 0
        assert m.output_tokens == 0

    def test_total_tokens_includes_cache(self):
        m = Meter(
            input_tokens=100, output_tokens=10,
            cache_read_tokens=1000, cache_write_tokens=50,
        )
        assert m.total_tokens == 1160


class TestPricing:
    def test_a_known_model_prices_out(self):
        # 1M in + 1M out on Opus 5 is $5 + $25.
        m = Meter(input_tokens=1_000_000, output_tokens=1_000_000)
        assert m.cost("claude-opus-5") == pytest.approx(30.0)

    def test_sonnet_is_cheaper_than_opus_for_the_same_work(self):
        m = Meter(input_tokens=30_000, output_tokens=4_000, web_searches=5)
        assert m.cost("claude-sonnet-5") < m.cost("claude-opus-5")

    def test_searches_are_priced(self):
        m = Meter(web_searches=10)
        assert m.cost("claude-opus-5") == pytest.approx(10 * WEB_SEARCH_COST)

    def test_cached_reads_cost_a_tenth_of_fresh_input(self):
        fresh = Meter(input_tokens=1_000_000).cost("claude-opus-5")
        cached = Meter(cache_read_tokens=1_000_000).cost("claude-opus-5")
        assert cached == pytest.approx(fresh * 0.1)

    def test_cache_writes_cost_a_quarter_more(self):
        fresh = Meter(input_tokens=1_000_000).cost("claude-opus-5")
        written = Meter(cache_write_tokens=1_000_000).cost("claude-opus-5")
        assert written == pytest.approx(fresh * 1.25)

    def test_an_unknown_model_reports_no_cost_rather_than_a_wrong_one(self):
        # Better to say nothing than to publish a confident wrong number.
        assert Meter(input_tokens=1000).cost("some-model-from-2029") is None

    def test_an_empty_run_costs_nothing(self):
        assert Meter().cost("claude-opus-5") == 0.0

    def test_every_model_the_readme_offers_has_a_price(self):
        for model in ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]:
            assert model in PRICES


class TestReport:
    def test_includes_the_dollar_figure_and_the_counts(self):
        m = Meter(calls=2, input_tokens=30_000, output_tokens=4_000, web_searches=5)
        text = m.report("claude-opus-5")

        assert "$0.300" in text
        assert "2 calls" in text
        assert "30,000 in" in text
        assert "5 searches" in text

    def test_omits_what_did_not_happen(self):
        text = Meter(calls=1, input_tokens=100, output_tokens=10).report("claude-opus-5")
        assert "search" not in text
        assert "cached" not in text

    def test_singular_and_plural_read_correctly(self):
        assert "1 call" in Meter(calls=1).report("claude-opus-5")
        assert "1 search)" in Meter(calls=1, web_searches=1).report("claude-opus-5")
        assert "2 searches" in Meter(calls=1, web_searches=2).report("claude-opus-5")

    def test_an_unknown_model_still_reports_the_counts(self):
        text = Meter(calls=1, input_tokens=500).report("unknown-model")
        assert "500 in" in text
        assert "$" not in text


class TestProjection:
    def test_scales_a_run_to_a_month(self):
        text = project(0.30, runs_per_day=24)
        assert "$7.20/day" in text
        assert "$216/month" in text

    def test_a_lower_cadence_costs_less(self):
        assert "$108" in project(0.30, runs_per_day=12)
