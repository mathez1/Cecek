"""Getting a picture onto X.

tweepy cannot do this. Its only media code targets
upload.twitter.com/1.1/media/upload.json, which X retired, so the upload half
is hand-rolled here against the v2 endpoints while tweepy still does the
posting. That leaves one posting flow talking to two hosts, which is odd but
is what the two APIs are.

The credentials are the OAuth 1.0a set the bot already holds. X's v2 media
endpoints accept OAuth 1.0a as an alternative to OAuth 2.0 user context, so
there is nothing new to authorise.

What is NOT established: whether X bills these calls. Its rate card has a
line for creating a post and one for alt-text metadata, and none for the
upload itself. Every call is counted and reported so the first real runs can
be checked against the developer console rather than guessed at.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

log = logging.getLogger(__name__)

BASE = "https://api.x.com/2/media"
UPLOAD = f"{BASE}/upload"
METADATA = f"{BASE}/metadata"

# Became required in May 2025, and omitting it is a common source of 400s.
CATEGORY = "tweet_image"

REQUEST_TIMEOUT = 60
CHUNK_BYTES = 2 * 1024 * 1024


class MediaError(RuntimeError):
    """The picture could not be uploaded. The post should go out without it."""


class MediaUploader:
    def __init__(self, cfg) -> None:
        self._auth = OAuth1(
            cfg.x_api_key,
            client_secret=cfg.x_api_secret,
            resource_owner_key=cfg.x_access_token,
            resource_owner_secret=cfg.x_access_token_secret,
            # OAuth 1.0a folds the body into the signature base string only
            # for form-urlencoded bodies. A multipart body must stay out of
            # it, which is this default; setting it True signs garbage.
            force_include_body=False,
        )
        self.calls = 0

    def upload(self, path: Path, alt: str = "") -> str:
        """Return a media id, or raise MediaError."""
        data = path.read_bytes()
        if not data:
            raise MediaError("the file is empty")

        try:
            media_id = self._simple(data)
        except MediaError as exc:
            # X has broken single-request image upload before and pushed
            # integrators onto the chunked route. One retry down that path
            # costs a few seconds and has saved the picture when it happens.
            log.info("simple upload failed (%s); trying chunked", exc)
            media_id = self._chunked(data)

        if alt:
            self._describe(media_id, alt)
        return media_id

    def _post(self, url: str, **kwargs) -> requests.Response:
        self.calls += 1
        try:
            return requests.post(
                url, auth=self._auth, timeout=REQUEST_TIMEOUT, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            raise MediaError(f"request to {url} failed: {exc}") from exc

    @staticmethod
    def _media_id(response: requests.Response, what: str) -> str:
        if response.status_code not in (200, 201):
            raise MediaError(
                f"{what} returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MediaError(f"{what} did not return JSON: {exc}") from exc

        # v2 nests the id under data and returns it as a string. Code ported
        # from v1.1 looks for a top-level media_id_string and finds nothing.
        media_id = str((body.get("data") or {}).get("id") or "")
        if not media_id:
            raise MediaError(f"{what} returned no media id: {str(body)[:200]}")
        return media_id

    def _simple(self, data: bytes) -> str:
        response = self._post(
            UPLOAD,
            files={"media": ("post.png", data, "image/png")},
            data={"media_category": CATEGORY},
        )
        media_id = self._media_id(response, "upload")
        log.info("uploaded %d bytes in one request", len(data))
        return media_id

    def _chunked(self, data: bytes) -> str:
        started = self._post(
            f"{UPLOAD}/initialize",
            json={
                "media_type": "image/png",
                "total_bytes": len(data),
                "media_category": CATEGORY,
            },
        )
        media_id = self._media_id(started, "initialize")

        for index, start in enumerate(range(0, len(data), CHUNK_BYTES)):
            chunk = data[start : start + CHUNK_BYTES]
            appended = self._post(
                f"{UPLOAD}/{media_id}/append",
                files={"media": ("post.png", chunk, "image/png")},
                data={"segment_index": str(index)},
            )
            if appended.status_code not in (200, 204):
                raise MediaError(
                    f"append {index} returned HTTP {appended.status_code}: "
                    f"{appended.text[:200]}"
                )

        finished = self._post(f"{UPLOAD}/{media_id}/finalize")
        if finished.status_code not in (200, 201):
            raise MediaError(
                f"finalize returned HTTP {finished.status_code}: "
                f"{finished.text[:200]}"
            )

        log.info("uploaded %d bytes in chunks", len(data))
        return media_id

    def _describe(self, media_id: str, alt: str) -> None:
        """Attach alt text. A failure here is not worth losing the picture."""
        response = self._post(
            METADATA,
            json={"id": media_id, "metadata": {"alt_text": {"text": alt}}},
        )
        if response.status_code not in (200, 201, 204):
            log.warning(
                "alt text rejected (HTTP %s): %s",
                response.status_code,
                response.text[:200],
            )
