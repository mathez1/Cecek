"""Durable memory for the account.

Two files, both committed back to the repo by the workflow so that the next
hourly run can read what the previous one did:

  memory/posts.jsonl  append-only log, one JSON object per published post
  memory/state.json   small counters (monthly usage, last run, consecutive errors)

Everything here degrades gracefully: a missing or corrupt file means "no
history", never a crash. A bot that cannot read its diary should still post.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def month_key(when: datetime | None = None) -> str:
    when = when or utc_now()
    return f"{when.year:04d}-{when.month:02d}"


@dataclass
class PostRecord:
    """One published (or dry-run) post."""

    text: str
    posted_at: str = field(default_factory=iso_now)
    topic: str = ""
    rationale: str = ""
    sources: list[str] = field(default_factory=list)
    tweet_id: str | None = None
    url: str | None = None
    dry_run: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PostRecord":
        known = {f: raw.get(f) for f in cls.__dataclass_fields__}
        known["text"] = known.get("text") or ""
        known["posted_at"] = known.get("posted_at") or iso_now()
        known["topic"] = known.get("topic") or ""
        known["rationale"] = known.get("rationale") or ""
        known["sources"] = known.get("sources") or []
        known["dry_run"] = bool(known.get("dry_run"))
        return cls(**known)


def load_posts(path: Path, limit: int | None = None) -> list[PostRecord]:
    """Read the post log newest-last. Corrupt lines are skipped, not fatal."""
    if not path.exists():
        return []

    records: list[PostRecord] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return []

    for line_no, line in enumerate(raw_lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(PostRecord.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("skipping malformed line %d in %s: %s", line_no, path, exc)

    if limit is None:
        return records
    if limit <= 0:
        # records[-0:] is the whole list, which is the opposite of what
        # "show me zero posts" asks for.
        return []
    return records[-limit:]


def append_post(path: Path, record: PostRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.to_json() + "\n")


DEFAULT_STATE: dict[str, Any] = {
    "last_run_at": None,
    "last_post_at": None,
    "last_status": None,
    "consecutive_failures": 0,
    "total_posts": 0,
    "monthly": {},
    # Model spend in dollars, per month, from the token counts the API
    # reports. Recorded for every run that called Claude, including the ones
    # that then failed to publish: those cost money too, and leaving them out
    # is how a bill surprises someone.
    "monthly_spend": {},
}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s); starting from a clean state", path, exc)
        return json.loads(json.dumps(DEFAULT_STATE))

    if not isinstance(data, dict):
        log.warning("%s is not a JSON object; starting from a clean state", path)
        return json.loads(json.dumps(DEFAULT_STATE))

    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged.update(data)

    if not isinstance(merged.get("monthly"), dict):
        merged["monthly"] = {}
    merged["monthly"] = {
        key: _as_int(value) for key, value in merged["monthly"].items()
    }

    if not isinstance(merged.get("monthly_spend"), dict):
        merged["monthly_spend"] = {}
    merged["monthly_spend"] = {
        key: _as_float(value) for key, value in merged["monthly_spend"].items()
    }

    # memory/README.md invites hand edits, so a null or a string here is a
    # realistic thing to find. Coerce once, rather than letting int() raise
    # somewhere downstream on every run until someone notices.
    for key in ("consecutive_failures", "total_posts"):
        merged[key] = _as_int(merged.get(key))

    return merged


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write atomically.

    This file is rewritten on every one of ~720 runs a month, and a run can be
    killed mid-write when the job hits its timeout. A half-written state.json
    parses as corrupt and silently resets the monthly budget counter, so the
    write goes to a temp file in the same directory and is renamed over the
    original, which is atomic on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def posts_this_month(state: dict[str, Any], when: datetime | None = None) -> int:
    return int(state.get("monthly", {}).get(month_key(when), 0))


def spend_this_month(state: dict[str, Any], when: datetime | None = None) -> float:
    return float(state.get("monthly_spend", {}).get(month_key(when), 0.0))


def record_spend(
    state: dict[str, Any], dollars: float | None, when: datetime | None = None
) -> None:
    """Add this run's model spend to the running monthly total."""
    if not dollars:
        return

    spend = state.setdefault("monthly_spend", {})
    key = month_key(when)
    spend[key] = round(float(spend.get(key, 0.0)) + float(dollars), 4)
    _prune(spend)


def record_success(state: dict[str, Any], when: datetime | None = None) -> None:
    when = when or utc_now()
    key = month_key(when)
    monthly = state.setdefault("monthly", {})
    monthly[key] = int(monthly.get(key, 0)) + 1
    state["total_posts"] = int(state.get("total_posts", 0)) + 1
    state["last_post_at"] = when.isoformat(timespec="seconds")
    state["last_status"] = "posted"
    state["consecutive_failures"] = 0
    _prune_monthly(state)


def record_skip(
    state: dict[str, Any], reason: str, *, counts_as_failure: bool = False
) -> None:
    """Note a run that did not publish but should not alarm anyone.

    A skip never clears the failure streak. Only a published post does, via
    record_success. A dry run or a budget stop says nothing about whether the
    credentials work, so clearing on one would re-arm the circuit breaker for
    another six expensive runs.

    Pass counts_as_failure=True when the skip cost real money and produced
    nothing, as an X rate limit does: the run still exits green because it
    fixes itself, but six in a row means the bot is stuck paying for posts it
    cannot publish, which is exactly what the breaker is for.
    """
    state["last_status"] = f"skipped: {reason}"
    if counts_as_failure:
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1


def record_failure(state: dict[str, Any], reason: str) -> None:
    state["last_status"] = f"failed: {reason}"
    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1


def touch_run(state: dict[str, Any], when: datetime | None = None) -> None:
    state["last_run_at"] = (when or utc_now()).isoformat(timespec="seconds")


def _prune_monthly(state: dict[str, Any], keep: int = 24) -> None:
    """Keep the state file small forever; it is committed on every run."""
    _prune(state.get("monthly", {}), keep)
    _prune(state.get("monthly_spend", {}), keep)


def _prune(buckets: dict[str, Any], keep: int = 24) -> None:
    if len(buckets) <= keep:
        return
    for key in sorted(buckets)[: len(buckets) - keep]:
        buckets.pop(key, None)


def recent_texts(records: Iterable[PostRecord]) -> list[str]:
    return [r.text for r in records if r.text]
