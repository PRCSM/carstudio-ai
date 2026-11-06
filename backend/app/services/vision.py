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
Use null for anything you are not confident about. Do not guess hidden details."""

COMPARE_PROMPT = """Image 1 is the original car photo; image 2 is a studio edit of it.
Compare only the vehicle. Return JSON only:
{"verdict": "same_vehicle"|"different_vehicle"|"uncertain",
 "changed_features": [str], "condition_changed": bool, "notes": str}
Flag changes to body shape, paint color, wheels, grille, lamps, badges, trim, accessories,
added/removed damage, or plate content. Lighting and background changes are expected."""

PROFILE_VERSION = "profile_v1"


class VisionClient:
    def __init__(self, api_key: str, model: str, endpoint: str = API, client: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.client = client or httpx.Client(timeout=60)

    def _call(self, prompt: str, images: list[bytes]) -> dict | None:
        parts = [{"text": prompt}] + [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(i).decode()}} for i in images]
        try:
            r = self.client.post(self.endpoint.format(model=self.model), headers={"x-goog-api-key": self.api_key},
                                 json={"contents": [{"role": "user", "parts": parts}],
                                       "generationConfig": {"responseMimeType": "application/json",
                                                            "temperature": 0}})
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:  # vision is advisory; never fail the job on it
            log.warning("vision call failed: %s", type(e).__name__)
            return None

    def profile(self, image: bytes) -> dict | None:
        return self._call(PROFILE_PROMPT, [image])

    def compare(self, source: bytes, output: bytes) -> dict | None:
        return self._call(COMPARE_PROMPT, [source, output])
