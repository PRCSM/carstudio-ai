"""Vision-model calls: vehicle profile extraction and source/output identity comparison.
Model guesses are never ground truth; low confidence means 'unknown'."""
import base64
import json
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
VERTEX_API = "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"

PROFILE_PROMPT = """Analyze this photo for an automotive studio-edit pipeline. Return JSON only:
{"is_car_photo": bool, "car_clearly_visible": bool, "make": str|null, "model": str|null,
 "body_type": str|null, "paint": str|null, "source_view": "front"|"rear"|"left"|"right"|
 "front_three_quarter"|"rear_three_quarter"|"other", "visible_features": [str],
 "visible_damage": [str], "unknown_features": [str], "confidence": "high"|"medium"|"low",
 "retake_reason": str|null}
Use null for anything you are not confident about. Do not guess hidden details.
Never transcribe license-plate text or any personal information."""

COMPARE_PROMPT = """Image 1 is the original car photo; image 2 is a studio edit of it.
Compare only the vehicle. Return JSON only:
{"verdict": "same_vehicle"|"different_vehicle"|"uncertain",
 "changed_features": [str], "condition_changed": bool, "notes": str}
Flag changes to body shape, paint color, wheels, grille, lamps, badges, trim, accessories,
added/removed damage, or plate content. Lighting and background changes are expected.
Never transcribe license-plate text or any personal information in your answer."""

PROFILE_VERSION = "profile_v1"


class VisionClient:
    def __init__(self, api_key: str, model: str, endpoint: str = API, client: httpx.Client | None = None):
        """`model` may be a comma-separated list; models that return 404 (retired / not offered on
        this endpoint) are skipped and the first working one is remembered."""
        self.api_key = api_key
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=60)

    @property
    def model(self) -> str | None:
        return self.models[0] if self.models else None

    def _call(self, prompt: str, images: list[bytes]) -> dict | None:
        parts = [{"text": prompt}] + [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(i).decode()}} for i in images]
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        while self.models:
            try:
                r = self.client.post(self.endpoint.format(model=self.models[0]),
                                     headers={"x-goog-api-key": self.api_key}, json=body)
                if r.status_code == 404:
                    log.warning("vision model %s not available; trying next", self.models[0])
                    self.models.pop(0)
                    continue
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception as e:  # vision is advisory; never fail the job on it
                log.warning("vision call failed: %s", type(e).__name__)
                return None
        log.error("no configured vision model is available")
        return None

    def profile(self, image: bytes) -> dict | None:
        return self._call(PROFILE_PROMPT, [image])

    def compare(self, source: bytes, output: bytes) -> dict | None:
        return self._call(COMPARE_PROMPT, [source, output])
