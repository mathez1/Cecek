"""Tests for the X error classification in bot/x_client.py.

The distinction that matters most here is that HTTP 429 means two different
things: a rate limit that clears on its own, and a usage cap that does not
clear until the billing cycle rolls. Treating them alike either spins a retry
loop for a month or takes the account offline for an hour.
"""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests
import tweepy

from bot.config import Config
from bot.x_client import (
    REQUEST_TIMEOUT,
    PostResult,
    XAuthError,
    XClient,
    XDuplicateError,
    XError,
    XQuotaError,
    XRateLimitError,
)


def fake_response(payload, status=400):
    return NS(
        status_code=status,
        reason="",
        json=lambda: payload,
        headers={},
    )


@pytest.fixture
def client():
    cfg = Config(
        anthropic_api_key="k",
        x_api_key="a", x_api_secret="b",
        x_access_token="c", x_access_token_secret="d",
    )
    return XClient(cfg)


def raise_on_post(client, exc):
    def boom(**kwargs):
        raise exc
    client.client.create_tweet = boom


class TestTimeout:
    def test_a_timeout_is_bound_onto_the_session(self, client):
        # tweepy exposes no timeout setting and sets none itself, so a stalled
        # api.twitter.com would block until the job is killed.
        assert client.client.session.request.keywords["timeout"] == REQUEST_TIMEOUT


class TestTheTwoKindsOf429:
    def test_a_rate_limit_is_retryable(self, client):
        exc = tweepy.errors.TooManyRequests(
            fake_response({"title": "Too Many Requests"}, 429), reset_time=1234
        )
        raise_on_post(client, exc)

        with pytest.raises(XRateLimitError) as caught:
            client.post("hello")
        assert caught.value.retryable is True
        assert caught.value.reset_time == 1234

    def test_a_usage_cap_is_not_retryable(self, client):
        # Same status code, completely different meaning: this one does not
        # clear until the billing cycle rolls over.
        exc = tweepy.errors.TooManyRequests(
            fake_response({
                "title": "UsageCapExceeded",
                "period": "Monthly",
                "scope": "Product",
                "detail": "Usage cap exceeded: Monthly product cap",
            }, 429)
        )
        raise_on_post(client, exc)

        with pytest.raises(XQuotaError) as caught:
            client.post("hello")
        assert caught.value.retryable is False
        assert "billing cycle" in str(caught.value)

    def test_the_cap_is_detected_from_the_detail_too(self, client):
        exc = tweepy.errors.TooManyRequests(
            fake_response({"detail": "Usage cap exceeded: Monthly product cap"}, 429)
        )
        raise_on_post(client, exc)
        with pytest.raises(XQuotaError):
            client.post("hello")


class TestForbidden:
    def test_duplicate_content_is_its_own_error(self, client):
        exc = tweepy.errors.Forbidden(fake_response({
            "detail": "You are not allowed to create a Tweet with duplicate content.",
            "title": "Forbidden",
        }, 403))
        raise_on_post(client, exc)

        with pytest.raises(XDuplicateError):
            client.post("hello")

    def test_other_403s_explain_the_usual_causes(self, client):
        exc = tweepy.errors.Forbidden(fake_response({
            "detail": "You are not permitted to perform this action.",
        }, 403))
        raise_on_post(client, exc)

        with pytest.raises(XAuthError) as caught:
            client.post("hello")
        # The message has to name the token-permission trap, because nothing
        # in X's own error does.
        assert "regenerating" in str(caught.value)


class TestTransportErrors:
    def test_a_connection_error_becomes_an_XError(self, client):
        # Regression: requests exceptions are NOT TweepyException subclasses,
        # so these used to escape every handler and kill the run with a raw
        # traceback instead of a recorded failure.
        raise_on_post(client, requests.exceptions.ConnectionError("Connection aborted"))

        with pytest.raises(XError) as caught:
            client.post("hello")
        assert caught.value.retryable is True

    def test_a_read_timeout_becomes_an_XError(self, client):
        raise_on_post(client, requests.exceptions.ReadTimeout("timed out"))
        with pytest.raises(XError):
            client.post("hello")

    def test_requests_exceptions_are_not_tweepy_exceptions(self):
        assert not issubclass(
            requests.exceptions.ConnectionError, tweepy.errors.TweepyException
        )


class TestUnauthorized:
    def test_401_mentions_clock_skew(self, client):
        exc = tweepy.errors.Unauthorized(fake_response({"title": "Unauthorized"}, 401))
        raise_on_post(client, exc)
        with pytest.raises(XAuthError) as caught:
            client.post("hello")
        assert "skewed" in str(caught.value)


class TestSuccess:
    def test_returns_the_id_and_a_url(self, client):
        client.client.create_tweet = lambda **kw: NS(data={"id": 123456, "text": "hi"})
        result = client.post("hi")

        assert isinstance(result, PostResult)
        assert result.tweet_id == "123456"
        assert result.url.endswith("/123456")

    def test_a_response_with_no_id_is_an_error(self, client):
        client.client.create_tweet = lambda **kw: NS(data={})
        with pytest.raises(XError, match="no id"):
            client.post("hi")
