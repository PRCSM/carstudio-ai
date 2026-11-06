"""Gemini native image editing (generateContent). Used twice in the chain: a billed key
(gemini_paid) and an unbilled free-tier key (gemini_free)."""
import base64

import httpx

from app.providers.base import (CreditExhausted, EditRequest, EditResult, PolicyRejected, QuotaExhausted,
                                TransientProviderError)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Vertex AI (billed through the normal Google Cloud account, so GCP credits apply).
VERTEX_API = "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"


class GeminiImage:
    def __init__(self, *, name: str, api_key: str, model: str, cost_usd: float, free_tier: bool,
                 endpoint: str = API, client: httpx.Client | None = None):
        self.name = name
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.cost_per_call_usd = cost_usd
        self.shares_data_for_training = free_tier
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=10))

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def edit(self, req: EditRequest) -> EditResult:
        body = {
            "contents": [{"role": "user", "parts": [
                {"text": req.prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(req.image).decode()}},
            ]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        try:
            # key goes in a header, never the URL, so it can't leak into request logs
            r = self.client.post(self.endpoint.format(model=self.model), json=body,
                                 headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException as e:
            raise TransientProviderError("gemini timeout") from e
        except httpx.HTTPError as e:
            raise TransientProviderError(f"gemini network error: {type(e).__name__}") from e

        text = r.text.lower()
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
                    return EditResult(image=base64.b64decode(inline["data"]), provider=self.name,
                                      model_id=self.model, cost_usd=self.cost_per_call_usd)
        raise TransientProviderError("gemini returned no image")
