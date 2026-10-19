"""Uploading a picture to X, and generating one in the first place.

Neither endpoint is reachable from a test, so both are faked at the requests
boundary. What is actually being checked is the error handling: these are the
two network calls in the bot that must never raise, because the post they are
decorating is worth more than the decoration.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

from bot import imagegen, media


class Cfg:
    x_api_key = "k"
    x_api_secret = "s"
    x_access_token = "t"
    x_access_token_secret = "ts"
    image_api_key = "img-key"
    image_model = "imagen-test"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


def uploaded(media_id="777"):
    return FakeResponse(200, {"data": {"id": media_id}})


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "post.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    return path


class TestUpload:
    def test_the_happy_path_returns_the_id_from_data(self, png, monkeypatch):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return uploaded("1234")

        monkeypatch.setattr(media.requests, "post", fake_post)

        assert media.MediaUploader(Cfg()).upload(png) == "1234"
        assert calls[0][0] == media.UPLOAD
        # media_category became required in 2025 and omitting it 400s.
        assert calls[0][1]["data"]["media_category"] == "tweet_image"

    def test_a_v1_shaped_response_is_not_silently_accepted(self, png, monkeypatch):
        # v1.1 returned media_id_string at the top level. Code that reads it
        # would get None here and post a tweet referencing nothing.
        monkeypatch.setattr(
            media.requests, "post",
            lambda url, **kw: FakeResponse(200, {"media_id_string": "999"}),
        )
        with pytest.raises(media.MediaError, match="no media id"):
            media.MediaUploader(Cfg()).upload(png)

    def test_it_falls_back_to_chunked_when_the_simple_route_fails(self, png, monkeypatch):
        seen = []

        def fake_post(url, **kwargs):
            seen.append(url)
            if url == media.UPLOAD:
                return FakeResponse(400, text="simple upload is having a day")
            return uploaded("55")

        monkeypatch.setattr(media.requests, "post", fake_post)

        assert media.MediaUploader(Cfg()).upload(png) == "55"
        assert seen == [
            media.UPLOAD,
            f"{media.UPLOAD}/initialize",
            f"{media.UPLOAD}/55/append",
            f"{media.UPLOAD}/55/finalize",
        ]

    def test_when_both_routes_fail_it_raises_rather_than_returns_junk(self, png, monkeypatch):
        monkeypatch.setattr(
            media.requests, "post", lambda url, **kw: FakeResponse(401, text="nope")
        )
        with pytest.raises(media.MediaError, match="401"):
            media.MediaUploader(Cfg()).upload(png)

    def test_a_network_error_becomes_a_media_error(self, png, monkeypatch):
        def boom(url, **kwargs):
            raise requests.exceptions.ConnectTimeout("no route")

        monkeypatch.setattr(media.requests, "post", boom)
        with pytest.raises(media.MediaError, match="failed"):
            media.MediaUploader(Cfg()).upload(png)

    def test_an_empty_file_never_reaches_the_network(self, tmp_path, monkeypatch):
        empty = tmp_path / "post.png"
        empty.write_bytes(b"")
        monkeypatch.setattr(
            media.requests, "post",
            lambda *a, **k: pytest.fail("should not have been called"),
        )
        with pytest.raises(media.MediaError, match="empty"):
            media.MediaUploader(Cfg()).upload(empty)

    def test_alt_text_is_sent_when_given(self, png, monkeypatch):
        bodies = {}

        def fake_post(url, **kwargs):
            bodies[url] = kwargs.get("json")
            return uploaded("42")

        monkeypatch.setattr(media.requests, "post", fake_post)
        media.MediaUploader(Cfg()).upload(png, alt="a rising curve")

        assert bodies[media.METADATA]["metadata"]["alt_text"]["text"] == "a rising curve"

    def test_rejected_alt_text_does_not_lose_the_picture(self, png, monkeypatch):
        def fake_post(url, **kwargs):
            if url == media.METADATA:
                return FakeResponse(403, text="not allowed")
            return uploaded("42")

        monkeypatch.setattr(media.requests, "post", fake_post)
        assert media.MediaUploader(Cfg()).upload(png, alt="something") == "42"

    def test_every_call_is_counted(self, png, monkeypatch):
        # X publishes no price for these, so the count is the only way to
        # check a bill against reality.
        monkeypatch.setattr(media.requests, "post", lambda url, **kw: uploaded())
        uploader = media.MediaUploader(Cfg())
        uploader.upload(png, alt="x")
        assert uploader.calls == 2


class TestIllustrator:
    def _ok(self, data=b"\x89PNG"):
        import base64
        return FakeResponse(
            200,
            {"predictions": [{"bytesBase64Encoded": base64.b64encode(data).decode()}]},
        )

    def test_no_key_means_no_call_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: pytest.fail("should not have been called"),
        )

        class NoKey(Cfg):
            image_api_key = ""

        assert imagegen.Illustrator(NoKey())("a bicycle", tmp_path / "p.png") is False

    @pytest.mark.parametrize(
        "prompt",
        [
            "a photograph of a bicycle",
            "photorealistic bicycle",
            "a bicycle, cinematic 8k",
        ],
    )
    def test_prompts_asking_for_realism_are_declined(self, prompt, tmp_path, monkeypatch):
        # A photoreal image of a real event is a deep fake under the EU AI
        # Act. The style suffix alone is not a guarantee against a prompt
        # pulling the other way.
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: pytest.fail("should not have been called"),
        )
        assert imagegen.Illustrator(Cfg())(prompt, tmp_path / "p.png") is False

    def test_a_generated_image_is_written_marked_and_counted(self, tmp_path, monkeypatch):
        sent = {}
        real = _one_real_png()

        def fake_post(url, **kwargs):
            sent.update(kwargs.get("json") or {})
            sent["url"] = url
            import base64
            return FakeResponse(
                200,
                {"predictions": [{"bytesBase64Encoded": base64.b64encode(real).decode()}]},
            )

        monkeypatch.setattr(imagegen.requests, "post", fake_post)

        path = tmp_path / "p.png"
        illustrator = imagegen.Illustrator(Cfg())
        assert illustrator("a bicycle leaning on a lamp post", path) is True
        assert illustrator.images == 1
        assert path.exists()

        prompt = sent["instances"][0]["prompt"]
        assert "bicycle" in prompt
        assert "not photographic" in prompt
        assert sent["parameters"]["personGeneration"] == "dont_allow"
        assert "imagen-test" in sent["url"]

    def test_the_marker_changes_the_pixels(self, tmp_path, monkeypatch):
        from PIL import Image

        real = _one_real_png()
        import base64
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(
                200,
                {"predictions": [{"bytesBase64Encoded": base64.b64encode(real).decode()}]},
            ),
        )

        path = tmp_path / "p.png"
        imagegen.Illustrator(Cfg())("a bicycle", path)

        with Image.open(path) as marked:
            corner = marked.convert("L").crop(
                (marked.width // 2, marked.height - 60, marked.width, marked.height)
            )
        # The source is a uniform mid-grey; the marker plate is black on white.
        low, high = corner.getextrema()
        assert low < 128 < high

    def test_an_http_error_is_not_an_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(429, text="slow down"),
        )
        assert imagegen.Illustrator(Cfg())("a bicycle", tmp_path / "p.png") is False

    def test_a_safety_filter_returning_nothing_is_not_an_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(200, {"predictions": []}),
        )
        assert imagegen.Illustrator(Cfg())("a bicycle", tmp_path / "p.png") is False

    def test_undecodable_data_is_not_an_exception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(200, {"predictions": [{"bytesBase64Encoded": "!!!"}]}),
        )
        assert imagegen.Illustrator(Cfg())("a bicycle", tmp_path / "p.png") is False

    def test_a_network_error_is_not_an_exception(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise requests.exceptions.ReadTimeout("slow")

        monkeypatch.setattr(imagegen.requests, "post", boom)
        assert imagegen.Illustrator(Cfg())("a bicycle", tmp_path / "p.png") is False

    def test_an_oversized_image_is_rejected_before_it_reaches_x(self, tmp_path, monkeypatch):
        import base64
        huge = b"\x89PNG" + b"0" * (imagegen.MAX_BYTES + 1)
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(
                200, {"predictions": [{"bytesBase64Encoded": base64.b64encode(huge).decode()}]}
            ),
        )
        path = tmp_path / "p.png"
        assert imagegen.Illustrator(Cfg())("a bicycle", path) is False
        assert not path.exists()

    def test_an_image_that_cannot_be_marked_is_thrown_away(self, tmp_path, monkeypatch):
        # Unmarked is not an option: the disclosure has to be in the pixels
        # because X strips C2PA metadata.
        import base64
        monkeypatch.setattr(
            imagegen.requests, "post",
            lambda *a, **k: FakeResponse(
                200,
                {"predictions": [{"bytesBase64Encoded": base64.b64encode(b"not a png").decode()}]},
            ),
        )
        path = tmp_path / "p.png"
        assert imagegen.Illustrator(Cfg())("a bicycle", path) is False
        assert not path.exists()


def _one_real_png() -> bytes:
    """A small uniform PNG, so the marker test has pixels to compare."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (640, 360), (128, 128, 128)).save(buffer, format="PNG")
    return buffer.getvalue()
