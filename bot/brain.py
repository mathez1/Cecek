"""The part that actually thinks.

Two calls to Claude per run, deliberately split:

  1. explore()  - web search and web fetch are on, output is free-form. The
                  model wanders wherever it wants and comes back with notes.
  2. compose()  - no tools, structured JSON out. Turns the notes into one post.

They are separate because forcing a rigid output schema onto the same call that
runs server-side tools makes the model cut its exploration short to satisfy the
schema. Letting it think freely first, then formatting second, produces
noticeably better posts and a response that is trivially parseable.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from bot.cost import Meter

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}

# Scalar server-side fallback form. Pairs only with this beta flag.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Both calls stream. The SDK refuses non-streaming requests that could run past
# ten minutes, and an exploration that really uses web search can sit well past
# any timeout worth setting on a single HTTP response. Streaming removes the
# question: the connection stays alive while the model works.
REQUEST_TIMEOUT = 660.0
TOTAL_BUDGET_SECONDS = 720.0

# Thinking tokens count toward max_tokens, so the ceiling has to move with
# effort or a high-effort run truncates mid-object and the JSON will not parse.
MAX_TOKENS_BY_EFFORT: dict[str, int] = {
    "low": 8_000,
    "medium": 16_000,
    "high": 24_000,
    "xhigh": 48_000,
    "max": 64_000,
}
DEFAULT_MAX_TOKENS = 24_000


def max_tokens_for(effort: str) -> int:
    return MAX_TOKENS_BY_EFFORT.get(effort, DEFAULT_MAX_TOKENS)

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "post": {
            "type": "string",
            "description": (
                "The post exactly as it should appear on X. At most 280 "
                "characters. No hashtags, no em dashes, no thread markers."
            ),
        },
        "topic": {
            "type": "string",
            "description": "Two to five words naming the subject, for your own future reference.",
        },
        "rationale": {
            "type": "string",
            "description": "One sentence: why this is worth saying right now.",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "URLs that back any specific claim in the post. Empty if none apply.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "How sure you are that every factual claim in the post is true.",
        },
    },
    "required": ["post", "topic", "rationale", "sources", "confidence"],
    "additionalProperties": False,
}


class BrainError(RuntimeError):
    """Claude could not produce something usable this run."""


@dataclass
class Exploration:
    notes: str
    sources: list[str] = field(default_factory=list)
    searched: bool = False


@dataclass
class Draft:
    post: str
    topic: str = ""
    rationale: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: str = "medium"


def _text_of(response: Any) -> str:
    """Join every text block. With server tools there are usually several."""
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip()


def _sources_of(response: Any) -> list[str]:
    """Pull result URLs out of web_search / web_fetch result blocks."""
    urls: list[str] = []

    for block in response.content:
        btype = getattr(block, "type", None)
        content = getattr(block, "content", None)

        if btype == "web_search_tool_result":
            # On success .content is a list of results; on error it is a single
            # object carrying an error_code. Branch before iterating.
            if isinstance(content, list):
                for item in content:
                    url = getattr(item, "url", None)
                    if url:
                        urls.append(url)
            else:
                log.warning(
                    "web search returned an error: %s",
                    getattr(content, "error_code", content),
                )

        elif btype == "web_fetch_tool_result":
            url = getattr(content, "url", None)
            if url:
                urls.append(url)
            elif content is not None and not hasattr(content, "url"):
                log.warning(
                    "web fetch returned an error: %s",
                    getattr(content, "error_code", content),
                )

    return _dedupe(urls)


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


class Brain:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        # The workflow kills the job at 15 minutes. The SDK's own retries
        # multiply against our retry loop below, so a generous per-request
        # timeout plus generous retries could spend well over an hour in here
        # and never reach the error handling at all. Keep the whole budget
        # comfortably inside the job's.
        self.client = anthropic.Anthropic(
            api_key=cfg.anthropic_api_key,
            timeout=REQUEST_TIMEOUT,
            max_retries=1,
        )
        self._deadline: float | None = None
        self._fallbacks_enabled = cfg.enable_refusal_fallback
        # Every call's usage lands here, so a run can price itself.
        self.meter = Meter()

    # ------------------------------------------------------------------ core

    def _create(self, **kwargs: Any) -> Any:
        """One Messages call, with refusal fallbacks and sane retries.

        Server-side fallbacks are best effort: if the account is not enrolled in
        the beta the API rejects the parameter, so we drop it once and carry on
        rather than letting an unattended run die over a nice-to-have.
        """
        attempts = 4
        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS

        for attempt in range(1, attempts + 1):
            if time.monotonic() > deadline:
                raise BrainError(
                    "gave up on the Anthropic API after "
                    f"{TOTAL_BUDGET_SECONDS:.0f}s of retries"
                )

            try:
                if self._fallbacks_enabled:
                    with self.client.beta.messages.stream(
                        betas=[FALLBACK_BETA],
                        fallbacks="default",
                        **kwargs,
                    ) as stream:
                        response = stream.get_final_message()
                else:
                    with self.client.messages.stream(**kwargs) as stream:
                        response = stream.get_final_message()

                self.meter.add(response)
                return response

            except anthropic.BadRequestError as exc:
                if self._fallbacks_enabled and _looks_like_fallback_rejection(exc):
                    log.warning(
                        "server-side refusal fallbacks unavailable on this account "
                        "(%s); continuing without them",
                        exc.message,
                    )
                    self._fallbacks_enabled = False
                    continue
                raise BrainError(f"the API rejected the request: {exc.message}") from exc

            except anthropic.AuthenticationError as exc:
                raise BrainError(
                    "ANTHROPIC_API_KEY was rejected. Check the repository secret."
                ) from exc

            except anthropic.PermissionDeniedError as exc:
                raise BrainError(
                    f"the API key lacks permission for this request: {exc}"
                ) from exc

            except anthropic.NotFoundError as exc:
                raise BrainError(
                    f"unknown model {kwargs.get('model')!r}: {exc}"
                ) from exc

            except anthropic.RateLimitError as exc:
                if attempt == attempts:
                    raise BrainError("rate limited by the Anthropic API") from exc
                delay = _retry_after(exc, attempt)
                log.warning("rate limited; sleeping %.0fs then retrying", delay)
                time.sleep(delay)

            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500 and attempt < attempts:
                    delay = _backoff(attempt)
                    log.warning(
                        "server error %s; sleeping %.0fs then retrying",
                        exc.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise BrainError(f"API error {exc.status_code}: {exc}") from exc

            except anthropic.APIConnectionError as exc:
                # APITimeoutError is a subclass, so this covers both.
                if attempt == attempts:
                    raise BrainError(f"could not reach the Anthropic API: {exc}") from exc
                delay = _backoff(attempt)
                log.warning("connection problem (%s); sleeping %.0fs", exc, delay)
                time.sleep(delay)

            except anthropic.AnthropicError as exc:
                # Catch-all for the rest of the SDK's hierarchy, notably
                # APIResponseValidationError: if the API ships a response shape
                # this client version cannot model, that must surface as a
                # recorded failure rather than a raw traceback at 03:23.
                raise BrainError(f"unexpected Anthropic SDK error: {exc}") from exc

        raise BrainError("exhausted retries talking to the Anthropic API")

    def _guard_stop_reason(self, response: Any, stage: str) -> None:
        reason = getattr(response, "stop_reason", None)
        if reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise BrainError(f"the model declined during {stage} (category: {category})")
        if reason == "max_tokens":
            log.warning("%s hit max_tokens; output may be truncated", stage)

    # ------------------------------------------------------------- stage one

    def explore(self, digest: str, recent_posts: list[str], persona: str) -> Exploration:
        """Let the model roam. Returns its notes, not a post."""
        tools = []
        if self.cfg.enable_web_search:
            tools = [WEB_SEARCH_TOOL, WEB_FETCH_TOOL]

        prompt = _explore_prompt(digest, recent_posts, self.cfg.enable_web_search)

        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": max_tokens_for(self.cfg.effort),
            "system": [
                {
                    "type": "text",
                    "text": persona,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.cfg.effort},
            "messages": [{"role": "user", "content": prompt}],
        }
        if tools:
            kwargs["tools"] = tools

        response = self._create(**kwargs)
        self._guard_stop_reason(response, "exploration")

        # Server tools run in a sampling loop that pauses after about ten
        # iterations. Resending the exchange resumes it; no extra user turn.
        #
        # Each paused response carries only ITS OWN segment of the turn, so the
        # text and the search results have to be accumulated as we go. Reading
        # only the final response throws away everything found before the last
        # pause, which is usually most of the exploration.
        messages = list(kwargs["messages"])
        note_parts = [_text_of(response)]
        all_sources = list(_sources_of(response))
        rounds = 0

        while (
            getattr(response, "stop_reason", None) == "pause_turn"
            and rounds < self.cfg.max_search_rounds
        ):
            rounds += 1
            log.info("exploration paused for more searching (round %d)", rounds)
            messages = messages + [{"role": "assistant", "content": response.content}]
            response = self._create(**{**kwargs, "messages": messages})
            self._guard_stop_reason(response, "exploration")

            note_parts.append(_text_of(response))
            all_sources.extend(_sources_of(response))

        if getattr(response, "stop_reason", None) == "pause_turn":
            log.warning("stopped exploring after %d rounds; using what we have", rounds)

        notes = "\n".join(part for part in note_parts if part).strip()
        if not notes:
            raise BrainError("exploration produced no text")

        sources = _dedupe(all_sources)
        log.info(
            "exploration done: %d chars of notes, %d sources",
            len(notes),
            len(sources),
        )
        return Exploration(notes=notes, sources=sources, searched=bool(tools))

    # ------------------------------------------------------------- stage two

    def compose(
        self,
        exploration: Exploration,
        recent_posts: list[str],
        persona: str,
        feedback: str | None = None,
    ) -> Draft:
        """Turn the notes into one post, as validated JSON."""
        prompt = _compose_prompt(exploration, recent_posts, feedback)

        # Adaptive thinking counts toward max_tokens, and at high effort it can
        # be far larger than the ~300 tokens of JSON we actually want. Being
        # generous costs nothing (max_tokens is a cap, not a charge) and avoids
        # a truncated response that will not parse.
        response = self._create(
            model=self.cfg.model,
            max_tokens=max_tokens_for(self.cfg.effort),
            system=[
                {
                    "type": "text",
                    "text": persona,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.cfg.effort,
                "format": {"type": "json_schema", "schema": DRAFT_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        self._guard_stop_reason(response, "composition")

        text = _text_of(response)
        if not text:
            raise BrainError("composition produced no text")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BrainError(f"composition did not return valid JSON: {exc}") from exc

        post = (data.get("post") or "").strip()
        if not post:
            raise BrainError("composition returned an empty post")

        sources = data.get("sources") or []
        if not isinstance(sources, list):
            sources = []

        return Draft(
            post=post,
            topic=(data.get("topic") or "").strip(),
            rationale=(data.get("rationale") or "").strip(),
            sources=[str(s) for s in sources if s],
            confidence=(data.get("confidence") or "medium").strip(),
        )


# ---------------------------------------------------------------- prompt text


def _explore_prompt(digest: str, recent_posts: list[str], can_search: bool) -> str:
    history = _format_history(recent_posts)

    search_clause = (
        "You have web search and web fetch. Use them. Follow whatever thread "
        "actually interests you, including threads that have nothing to do with "
        "the headlines below. Chase the second-order question, not the headline "
        "itself. If a claim hinges on a number or a date, look it up rather than "
        "trusting your memory."
        if can_search
        else "You have no web access this run, so work from the feed below and "
        "from what you already know. Prefer ideas that do not hinge on a fact "
        "you cannot verify."
    )

    return f"""This is your hourly run. Nobody is going to review what you write.

{search_clause}

Here is what some feeds are carrying right now. It is raw material, not an
assignment. You are free to ignore all of it.

{digest or "(no feed items available this run)"}

{history}

Think out loud, then finish with a section headed EXACTLY:

CANDIDATES

Under it, list two or three things you could post about. For each one give:
- the idea in a sentence
- the specific detail that makes it worth anyone's time (a number, a name, a
  year, a mechanism)
- how confident you are that the detail is actually true, and what you checked
- why it is not just a restatement of something you already posted

Then add a line starting with PICK: naming which one you want and why.

Do not write the post itself yet."""


def _compose_prompt(
    exploration: Exploration, recent_posts: list[str], feedback: str | None
) -> str:
    history = _format_history(recent_posts)

    retry_clause = ""
    if feedback:
        retry_clause = f"""
An earlier draft was rejected. Do not repeat the mistake.

{feedback}
"""

    return f"""Here are the notes you just made while exploring:

{exploration.notes}

{history}
{retry_clause}
Now write the post.

One post. At most 280 characters. Take the idea you picked and write the
sharpest version of it. Cut every word that is not carrying weight. If the
result is boring, pick a different idea from your notes rather than dressing
this one up.

Set confidence to "low" if any factual claim in the post is something you did
not verify this run. Be honest about that. It is used to decide whether the
post goes out.

Return the JSON object and nothing else."""


def _format_history(recent_posts: list[str]) -> str:
    if not recent_posts:
        return "You have not posted anything yet. This is your first post."

    lines = "\n".join(f"- {p}" for p in reversed(recent_posts))
    return f"""Your recent posts, newest first. Do not repeat these, and do not
write a variation on one of them:

{lines}"""


# -------------------------------------------------------------------- helpers


def _looks_like_fallback_rejection(exc: anthropic.BadRequestError) -> bool:
    """Only the fallback parameter itself.

    Matching a bare "beta" would swallow any other 400 whose message happens to
    echo the beta header, silently turn fallbacks off, and retry the real
    problem several times before surfacing it.
    """
    blob = f"{getattr(exc, 'message', '')} {exc}".lower()
    return "fallback" in blob


def _backoff(attempt: int) -> float:
    return min(60.0, (2 ** attempt) + random.uniform(0, 1.5))


def _retry_after(exc: anthropic.RateLimitError, attempt: int) -> float:
    try:
        header = exc.response.headers.get("retry-after")
        if header:
            return min(120.0, float(header))
    except (AttributeError, TypeError, ValueError):
        pass
    return _backoff(attempt)
