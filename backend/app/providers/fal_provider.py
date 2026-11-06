"""FLUX Kontext on fal.ai (synchronous endpoint)."""
import base64

import httpx

from app.providers.base import (CreditExhausted, EditRequest, EditResult, PolicyRejected, QuotaExhausted,
                                TransientProviderError)


class FalFluxKontext:
    name = "fal_flux_kontext"
    shares_data_for_training = False
    supports_multi_reference = False

    def __init__(self, api_key: str, model: str, cost_usd: float, client: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.cost_per_call_usd = cost_usd
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=10))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def edit(self, req: EditRequest) -> EditResult:
        image_ref = req.image_url or "data:image/jpeg;base64," + base64.b64encode(req.image).decode()
        try:
            r = self.client.post(
                f"https://fal.run/{self.model}",
                headers={"Authorization": f"Key {self.api_key}"},
                json={"prompt": req.prompt, "image_url": image_ref, "num_images": 1,
                      "output_format": "jpeg", "safety_tolerance": "2"},
            )
        except httpx.TimeoutException as e:
            raise TransientProviderError("fal timeout") from e
        except httpx.HTTPError as e:
            raise TransientProviderError(f"fal network error: {type(e).__name__}") from e

        if r.status_code in (401, 402, 403):
            raise CreditExhausted(f"fal {r.status_code}")  # bad key or exhausted balance
        if r.status_code == 429:
            raise QuotaExhausted("fal rate limited")
        if r.status_code == 422 and "content" in r.text.lower():
            raise PolicyRejected("fal content policy")
        if r.status_code >= 400:
            raise TransientProviderError(f"fal {r.status_code}")

        body = r.json()
        if body.get("has_nsfw_concepts") and any(body["has_nsfw_concepts"]):
            raise PolicyRejected("fal flagged output")
        images = body.get("images") or []
        if not images:
            raise TransientProviderError("fal returned no image")
        img = self.client.get(images[0]["url"])
        if img.status_code >= 300:
            raise TransientProviderError("fal result download failed")
        return EditResult(image=img.content, provider=self.name, model_id=self.model,
                          cost_usd=self.cost_per_call_usd, meta={"seed": body.get("seed")})
