"""2K output from the Gemini image provider: imageConfig, model fallback, per-model pricing."""
import base64
import io
import json

import numpy as np
import pytest
from PIL import Image

httpx = pytest.importorskip("httpx")

from app.providers.base import EditRequest  # noqa: E402
from app.providers.gemini_provider import GeminiImage, nearest_aspect, supports_image_size  # noqa: E402
from tests.conftest import to_jpeg  # noqa: E402


# ---------- 2K output from Gemini ----------
def test_nearest_aspect():
    assert nearest_aspect(1600, 900) == "16:9"
    assert nearest_aspect(1200, 900) == "4:3"
    assert nearest_aspect(900, 1600) == "9:16"
    assert nearest_aspect(1000, 1000) == "1:1"
    assert nearest_aspect(0, 0) == "1:1"


def test_image_size_only_for_gemini3_models():
    assert supports_image_size("gemini-3-pro-image-preview")
    assert supports_image_size("gemini-3.1-flash-image-preview")
    assert not supports_image_size("gemini-2.5-flash-image")


def _png_b64(w=64, h=36):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _provider(handler, models="gemini-3-pro-image-preview,gemini-2.5-flash-image"):
    return GeminiImage(name="vertex_gemini", api_key="k", model=models, cost_usd=0.039, free_tier=False,
                       image_size="2K", model_prices={"pro-image": 0.134},
                       client=httpx.Client(transport=httpx.MockTransport(handler)))


def _req():
    img = to_jpeg(np.full((900, 1600, 3), 120, np.uint8))
    return EditRequest(image=img, prompt="p", style="white_studio", width=1600, height=900)


def _ok():
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": _png_b64()}}]}}]})


def test_requests_2k_with_nearest_aspect_and_bills_pro_price():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return _ok()

    res = _provider(handler).edit(_req())
    path, body = seen[0]
    assert "gemini-3-pro-image-preview" in path
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert res.model_id == "gemini-3-pro-image-preview" and res.cost_usd == 0.134
    assert res.meta["output_size"] == [64, 36]


def test_falls_back_to_next_model_on_404_and_omits_image_size_for_25():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if "gemini-3-pro" in request.url.path:
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return _ok()

    p = _provider(handler)
    res = p.edit(_req())
    assert res.model_id == "gemini-2.5-flash-image" and res.cost_usd == 0.039
    assert seen[-1][1]["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9"}
    # the working model is remembered: the 404 model isn't tried again
    p.edit(_req())
    assert sum("gemini-3-pro" in path for path, _ in seen) == 1


def test_retries_without_image_config_when_rejected():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "imageConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Unknown field imageConfig"}})
        return _ok()

    res = _provider(handler, models="gemini-3-pro-image-preview").edit(_req())
    assert len(seen) == 2 and "imageConfig" not in seen[1]["generationConfig"]
    assert res.meta["image_config"] is False


def test_budget_precheck_uses_most_expensive_model():
    p = _provider(lambda r: _ok())
    assert p.cost_per_call_usd == 0.134
