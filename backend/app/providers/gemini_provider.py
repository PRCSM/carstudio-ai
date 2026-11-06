"""Gemini native image editing (generateContent). Used for Vertex AI (GCP credits), a billed AI Studio key
(gemini_paid) and an unbilled free-tier key (gemini_free).

`model` may be a comma-separated list, best first. A model that answers 404 (not offered on this endpoint
or retired) is skipped and the first working one is remembered. Output resolution is requested through
generationConfig.imageConfig (imageSize "2K"/"4K" on the Gemini 3 image models); if a model rejects that
field we retry once without it."""
import base64
import io
import logging
import math

import httpx

from app.providers.base import (CreditExhausted, EditRequest, EditResult, PolicyRejected, QuotaExhausted,
                                TransientProviderError)

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Vertex AI (billed through the normal Google Cloud account, so GCP credits apply).
VERTEX_API = "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"

# Aspect ratios every Gemini image model accepts.
ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]


def nearest_aspect(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "1:1"
    target = math.log(width / height)

    def dist(r: str) -> float:
        a, b = (int(x) for x in r.split(":"))
        return abs(math.log(a / b) - target)

    return min(ASPECT_RATIOS, key=dist)


def supports_image_size(model: str) -> bool:
    """imageSize (1K/2K/4K) exists on the Gemini 3 image models; 2.x image models are 1K only."""
    return not model.startswith(("gemini-2.", "gemini-2-"))


def _image_dims(jpeg: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as im:
            return im.size
    except Exception:
        return 0, 0


class GeminiImage:
    def __init__(self, *, name: str, api_key: str, model: str, cost_usd: float, free_tier: bool,
                 endpoint: str = API, client: httpx.Client | None = None, image_size: str = "",
                 model_prices: dict[str, float] | None = None):
        self.name = name
        self.api_key = api_key
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self._working: str | None = None
        self.endpoint = endpoint
        self.image_size = image_size.strip().upper()
        self.supports_multi_reference = True
        self.model_prices = model_prices or {}
        self.default_cost = cost_usd
        # Budget pre-check uses the most expensive model we might end up calling.
        self.cost_per_call_usd = max([cost_usd, *[self._price(m) for m in self.models]]) if cost_usd else 0.0
        self.shares_data_for_training = free_tier
        self.client = client or httpx.Client(timeout=httpx.Timeout(240, connect=10))

    @property
    def model(self) -> str:
        return self._working or (self.models[0] if self.models else "")

    def _price(self, model: str) -> float:
        if not self.default_cost:
            return 0.0  # free tier
        for key, price in self.model_prices.items():
            if key and key in model:
                return price
        return self.default_cost

    def is_configured(self) -> bool:
        return bool(self.api_key) and bool(self.models)

    def _body(self, req: EditRequest, model: str, *, with_image_config: bool) -> dict:
        gen: dict = {"responseModalities": ["IMAGE"]}
        if with_image_config:
            w, h = _image_dims(req.image)
            cfg = {"aspectRatio": nearest_aspect(w or req.width, h or req.height)}
            if self.image_size and supports_image_size(model):
                cfg["imageSize"] = self.image_size
            gen["imageConfig"] = cfg
        return {
            "contents": [{"role": "user", "parts": [
                {"text": req.prompt},
                *[{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}
                  for img in (req.image, *req.extra_images)],
            ]}],
            "generationConfig": gen,
        }

    def _post(self, model: str, body: dict) -> httpx.Response:
        try:
            # key goes in a header, never the URL, so it can't leak into request logs
            return self.client.post(self.endpoint.format(model=model), json=body,
                                    headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException as e:
            raise TransientProviderError("gemini timeout") from e
        except httpx.HTTPError as e:
            raise TransientProviderError(f"gemini network error: {type(e).__name__}") from e

    def edit(self, req: EditRequest) -> EditResult:
        candidates = [self._working] if self._working else list(self.models)
        last_404 = None
        for model in candidates:
            with_cfg = True
            r = self._post(model, self._body(req, model, with_image_config=True))
            text = r.text.lower()
            if r.status_code == 400 and any(k in text for k in ("imageconfig", "image_config", "imagesize",
                                                               "image_size", "aspect")):
                log.warning("%s rejected imageConfig; retrying without it", model)
                with_cfg = False
                r = self._post(model, self._body(req, model, with_image_config=False))
                text = r.text.lower()
            if r.status_code == 404:
                log.warning("image model %s not available on %s; trying next", model, self.name)
                last_404 = model
                continue
            self._working = model
            return self._parse(r, text, model, with_cfg)
        raise CreditExhausted(f"no image model available (last 404: {last_404})")

    def _parse(self, r: httpx.Response, text: str, model: str, with_cfg: bool) -> EditResult:
        if r.status_code == 429:
            raise QuotaExhausted("gemini quota exhausted")
        if r.status_code in (401, 402, 403) or (r.status_code == 400 and ("billing" in text or "api key" in text)):
            raise CreditExhausted(f"gemini {r.status_code}")
        if r.status_code >= 400:
            raise TransientProviderError(f"gemini {r.status_code}")

        data = r.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise PolicyRejected("gemini blocked prompt")
        for cand in data.get("candidates", []):
            if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY"):
                raise PolicyRejected("gemini safety block")
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    image = base64.b64decode(inline["data"])
                    w, h = _image_dims(image)
                    return EditResult(image=image, provider=self.name, model_id=model,
                                      cost_usd=self._price(model),
                                      meta={"output_size": [w, h], "requested_size": self.image_size or "default",
                                            "image_config": with_cfg})
        raise TransientProviderError("gemini returned no image")
