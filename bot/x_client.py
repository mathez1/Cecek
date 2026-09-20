"""Posting to X.

Uses OAuth 1.0a user context deliberately. OAuth 2.0 PKCE needs a browser for
the first authorization and then a refresh token that rotates on every use and
must be persisted; lose it and a human has to go back to a browser. OAuth 1.0a
is four static strings that do not expire, which is what an unattended hourly
job actually needs.

The important subtlety here is that HTTP 429 means two different things:

  - a rate limit, which clears at x-rate-limit-reset (minutes or hours), and
  - UsageCapExceeded, which does not clear until the billing cycle rolls over.

Code that retries blindly on 429 spins forever on the second kind. We branch on
the response body's `title`, not on the status code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import tweepy

log = logging.getLogger(__name__)

POST_URL_TEMPLATE = "https://x.com/i/web/status/{tweet_id}"


class XError(RuntimeError):
    """Posting failed."""

    retryable = False


class XAuthError(XError):
    """Credentials are wrong, revoked, or minted under the old permission level."""


class XDuplicateError(XError):
    """X thinks this is a duplicate of something recently posted."""


class XRateLimitError(XError):
    """A real rate limit. Clears on its own; the next cron tick can try again."""

    retryable = True

    def __init__(self, message: str, reset_time: float | None = None) -> None:
        super().__init__(message)
        self.reset_time = reset_time


class XQuotaError(XError):
    """Monthly usage cap or exhausted pay-as-you-go balance. Needs a human."""


@dataclass
class PostResult:
    tweet_id: str
    text: str

    @property
    def url(self) -> str:
        return POST_URL_TEMPLATE.format(tweet_id=self.tweet_id)


def _response_payload(exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    if response is None:
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _describe(exc: Exception) -> str:
    """X's useful detail lives in the body, not the status line. Surface it."""
    payload = _response_payload(exc)
    parts = [str(payload.get(key)) for key in ("title", "detail") if payload.get(key)]

    errors = payload.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                message = err.get("message") or err.get("detail")
                if message:
                    parts.append(str(message))

    return " | ".join(parts) if parts else str(exc)


class XClient:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        # wait_on_rate_limit is deliberately off: tweepy implements it with an
        # inline time.sleep() until the reset timestamp, which on a 24 hour
        # window blocks the runner for hours. Failing fast and letting the next
        # hourly run try again is the correct behaviour for a cron job.
        self.client = tweepy.Client(
            consumer_key=cfg.x_api_key,
            consumer_secret=cfg.x_api_secret,
            access_token=cfg.x_access_token,
            access_token_secret=cfg.x_access_token_secret,
            wait_on_rate_limit=False,
        )

    def post(self, text: str) -> PostResult:
        try:
            response = self.client.create_tweet(text=text)

        except tweepy.errors.TooManyRequests as exc:
            payload = _response_payload(exc)
            title = str(payload.get("title", ""))
            detail = str(payload.get("detail", ""))

            if "usagecap" in title.lower().replace(" ", "") or "usage cap" in detail.lower():
                raise XQuotaError(
                    "X usage cap exceeded: "
                    + _describe(exc)
                    + ". This does not reset until the billing cycle rolls over "
                    "or you add credit."
                ) from exc

            raise XRateLimitError(
                f"rate limited by X: {_describe(exc)}",
                reset_time=getattr(exc, "reset_time", None),
            ) from exc

        except tweepy.errors.Forbidden as exc:
            detail = _describe(exc)
            lowered = detail.lower()

            if "duplicate" in lowered:
                raise XDuplicateError(f"X rejected the post as a duplicate: {detail}") from exc

            raise XAuthError(
                f"X returned 403: {detail}. Common causes: the app is not "
                "attached to a Project; app permissions are not Read and write; "
                "the access token was generated before permissions were changed "
                "and needs regenerating; or the account has no credit balance."
            ) from exc

        except tweepy.errors.Unauthorized as exc:
            raise XAuthError(
                f"X returned 401: {_describe(exc)}. The four X_* secrets are "
                "wrong, revoked, or the runner's clock is skewed."
            ) from exc

        except tweepy.errors.BadRequest as exc:
            raise XError(f"X rejected the request: {_describe(exc)}") from exc

        except tweepy.errors.TwitterServerError as exc:
            err = XError(f"X is having server trouble: {_describe(exc)}")
            err.retryable = True
            raise err from exc

        except tweepy.errors.TweepyException as exc:
            raise XError(f"posting to X failed: {_describe(exc)}") from exc

        data = getattr(response, "data", None) or {}
        tweet_id = str(data.get("id", "")) if isinstance(data, dict) else ""
        if not tweet_id:
            raise XError(f"X accepted the post but returned no id: {response!r}")

        result = PostResult(tweet_id=tweet_id, text=text)
        log.info("posted %s", result.url)
        return result
