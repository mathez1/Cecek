"""Generated illustrations, for the posts no chart can carry.

Claude is text-only, so this is the one place the bot talks to a second
vendor, and the only part of a run that can spend money without producing a
post. It is therefore off unless a key is present, capped per day, and
written so that every failure is a quiet False rather than an exception.

Two deliberate constraints on what it draws:

The style suffix forces a flat monochrome illustration. That matches the
avatar, but the reason it is not negotiable is that a photoreal image of a
real news event is a deepfake under the EU AI Act's Article 3(60), with
disclosure duties that have applied since 2 August 2026. An obvious drawing
is not.

The visible marker is burned into the pixels because C2PA provenance metadata
does not survive upload to X. A label that the platform strips is not a
label.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:predict"
)

# Appended to every prompt. The model is told what to draw; this says how.
STYLE = (
    " Flat two-colour line illustration, pure white strokes on a plain black "
    "background, thin even line weight, minimal geometric shapes, generous "
    "empty space, no text, no lettering, no logos, no signature, no gradient, "
    "no shading, no texture, not photographic, not realistic."
)

# Words that would push the model toward photorealism whatever the style
# suffix says. The schema asks the model not to use them; this is the check
# that it did not.
_PHOTOREAL = re.compile(
    r"\b(photo|photos|photograph|photographic|photorealistic|realistic|"
    r"render|4k|8k|dslr|cinematic|lifelike)\b",
    re.IGNORECASE,
)

REQUEST_TIMEOUT = 60
MAX_BYTES = 5 * 1024 * 1024  # X rejects images past 5 MB on the simple route


class Illustrator:
    """Callable that writes one PNG, or returns False having written nothing."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.images = 0  # what cost.py prices

    def __call__(self, prompt: str, path: Path) -> bool:
        if not self.cfg.image_api_key:
            return False

        if _PHOTOREAL.search(prompt):
            log.info("declining an illustration prompt that asks for realism")
            return False

        try:
            data = self._generate(prompt + STYLE)
        except requests.exceptions.RequestException as exc:
            # Network trouble at a vendor that is not on the critical path is
            # not worth failing a run over.
            log.warning("image request failed: %s", exc)
            return False
        except ImageError as exc:
            log.warning("no image came back: %s", exc)
            return False

        if len(data) > MAX_BYTES:
            log.warning("generated image is %d bytes, over the limit", len(data))
            return False

        path.write_bytes(data)
        self.images += 1

        try:
            _mark(path)
        except Exception as exc:  # noqa: BLE001
            # An unmarked AI image is not one this bot will post, so the
            # failure has to take the picture with it.
            log.warning("could not mark the image as generated: %s", exc)
            path.unlink(missing_ok=True)
            return False

        return True

    def _generate(self, prompt: str) -> bytes:
        response = requests.post(
            ENDPOINT.format(model=self.cfg.image_model),
            headers={"x-goog-api-key": self.cfg.image_api_key},
            json={
                "instances": [{"prompt": prompt}],
                "parameters": {
                    "sampleCount": 1,
                    "aspectRatio": "16:9",
                    # No synthetic people. Likeness of a real person is the
                    # one failure mode here with a named victim.
                    "personGeneration": "dont_allow",
                },
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise ImageError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            predictions = response.json().get("predictions") or []
        except ValueError as exc:
            raise ImageError(f"response was not JSON: {exc}") from exc

        if not predictions:
            # Usually a safety filter. The body says which, and it is short.
            raise ImageError(f"no predictions: {response.text[:200]}")

        encoded = predictions[0].get("bytesBase64Encoded")
        if not encoded:
            raise ImageError("prediction carried no image data")

        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageError(f"image data would not decode: {exc}") from exc


class ImageError(RuntimeError):
    """The vendor answered, but not with a usable image."""


MARK = "AI-GENERATED"


def _mark(path: Path) -> None:
    """Burn the disclosure into the corner of the pixels."""
    from PIL import Image, ImageDraw, ImageFont
    from matplotlib import font_manager

    with Image.open(path) as source:
        image = source.convert("RGB")

    draw = ImageDraw.Draw(image)
    size = max(14, image.height // 38)
    try:
        font = ImageFont.truetype(
            font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans")),
            size,
        )
    except OSError:
        font = ImageFont.load_default()

    pad = max(10, image.height // 48)
    box = draw.textbbox((0, 0), MARK, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    x, y = image.width - width - pad, image.height - height - pad * 2

    # A plate behind the text, because the corner of a generated image can be
    # any colour and white-on-white is not a disclosure.
    draw.rectangle(
        [x - pad // 2, y - pad // 2, x + width + pad // 2, y + height + pad],
        fill=(0, 0, 0),
    )
    draw.text((x, y), MARK, font=font, fill=(255, 255, 255))
    image.save(path, format="PNG")
