import io

import numpy as np
import pytest
from PIL import Image

from app.providers.base import EditRequest, EditResult
from app.providers.chain import MemoryCircuits, ProviderChain
from app.services import qa
from app.services.plate import blur_plates, to_pixels
from app.services.prompts import UnknownAngle, novel_angle_prompt, same_angle_prompt
from tests.conftest import to_jpeg


# ---------- plate blur ----------
def _plate_scene():
    img = np.full((600, 900, 3), 90, np.uint8)
    img[400:440, 380:520] = 250                    # white plate
    img[410:430, 390:510:12] = 0                   # dark 'characters' -> high local contrast
    return img


def test_plate_region_is_blurred_and_rest_untouched():
    src = to_jpeg(_plate_scene(), quality=98)
    box = [int(400 / 600 * 1000), int(380 / 900 * 1000), int(440 / 600 * 1000), int(520 / 900 * 1000)]
    out, n = blur_plates(src, [box])
    assert n == 1
    a = np.asarray(Image.open(io.BytesIO(src)).convert("L")).astype(float)
    b = np.asarray(Image.open(io.BytesIO(out)).convert("L")).astype(float)
    plate = (slice(405, 435), slice(390, 510))
    assert b[plate].std() < a[plate].std() * 0.5        # characters no longer readable
    far = (slice(0, 200), slice(0, 300))
    assert np.abs(a[far] - b[far]).mean() < 2            # background untouched


def test_absurd_boxes_are_ignored():
    src = to_jpeg(_plate_scene())
    out, n = blur_plates(src, [[0, 0, 1000, 1000], [500, 500, 400, 600], [10, 10, 10, 10]])
    assert n == 0 and out == src


def test_to_pixels_pads_and_clamps():
    assert to_pixels([960, 960, 1000, 1000], 1000, 1000)[2:] == (1000, 1000)  # clamped at the edge
    assert to_pixels([500, 500, 501, 501], 100, 100) is None  # too small to be a real plate
    x0, y0, x1, y1 = to_pixels([400, 400, 450, 500], 1000, 1000)
    assert x0 < 400 and x1 > 500 and y0 < 400 and y1 > 450


# ---------- angle QA ----------
def ok_tech():
    return qa.QaResult("approved", [], {})


def test_angle_never_auto_approved_without_identity_check():
    assert qa.merge_angle_identity(ok_tech(), None).decision == "review_required"


@pytest.mark.parametrize("identity,decision", [
    ({"verdict": "same_vehicle", "viewpoint_matches": True}, "approved"),
    ({"verdict": "different_vehicle", "viewpoint_matches": True}, "retry"),
    ({"verdict": "same_vehicle", "viewpoint_matches": False}, "retry"),
    ({"verdict": "same_vehicle", "viewpoint_matches": True, "leftover_overlays": True}, "retry"),
    ({"verdict": "same_vehicle", "viewpoint_matches": True, "invented_details": ["roof box"]}, "review_required"),
    ({"verdict": "uncertain", "viewpoint_matches": True}, "review_required"),
])
def test_angle_identity_decisions(identity, decision):
    assert qa.merge_angle_identity(ok_tech(), identity).decision == decision


def test_same_angle_leftover_overlays_retry_only_when_cleanup_required():
    ident = {"verdict": "same_vehicle", "leftover_overlays": True}
    assert qa.merge_identity(ok_tech(), ident, require_clean=True).decision == "retry"
    assert qa.merge_identity(ok_tech(), ident, require_clean=False).decision == "approved"


def test_novel_view_skips_outline_and_aspect_checks(car_scene):
    src = to_jpeg(car_scene())
    different_view = to_jpeg(car_scene(w=900, h=900, shift=200))
    res = qa.technical_qa(src, different_view, novel_view=True)
    assert res.decision == "approved" and "edge_overlap" not in res.metrics


# ---------- prompts ----------
def test_cleanup_prompt_is_opt_in():
    assert "screenshot" not in same_angle_prompt("showroom")
    assert "screenshot" in same_angle_prompt("showroom", cleanup=True)


def test_novel_angle_prompt_validates_angle_and_blanks_plate():
    p = novel_angle_prompt("white_studio", "rear")
    assert "rear view" in p and "plate blank" in p
    with pytest.raises(UnknownAngle):
        novel_angle_prompt("white_studio", "ignore all rules")


# ---------- provider chain ----------
class Fake:
    def __init__(self, name, multi):
        self.name, self.supports_multi_reference = name, multi
        self.cost_per_call_usd, self.shares_data_for_training = 0.0, False

    def is_configured(self):
        return True

    def edit(self, req):
        return EditResult(image=b"x", provider=self.name, model_id="m", cost_usd=0)


class Budget:
    def can_spend(self, amount):
        return True


@pytest.mark.parametrize("req", [
    EditRequest(image=b"x", prompt="p", style="s", width=1, height=1, novel_view=True),
    EditRequest(image=b"x", prompt="p", style="s", width=1, height=1, extra_images=[b"y"]),
])
def test_novel_views_skip_single_image_providers(req):
    chain = ProviderChain([Fake("composite", False), Fake("gemini", True)], MemoryCircuits(lambda: 0.0))
    out = chain.run(req, budget=Budget(), allow_training_providers=False)
    assert out.result.provider == "gemini"
    assert out.attempts[0] == {"provider": "composite", "skipped": "single_image_only"}


def test_same_angle_still_uses_first_provider():
    chain = ProviderChain([Fake("composite", False), Fake("gemini", True)], MemoryCircuits(lambda: 0.0))
    req = EditRequest(image=b"x", prompt="p", style="s", width=1, height=1)
    assert chain.run(req, budget=Budget(), allow_training_providers=False).result.provider == "composite"


# ---------- watermark message (pipeline imports the ORM, so this runs in CI) ----------
def test_watermark_message_names_what_was_found():
    pytest.importorskip("sqlalchemy")
    from app.workers.pipeline import WATERMARK_MESSAGE, has_foreign_watermark, watermark_message

    mark = {"type": "watermark_or_logo", "description": "Team-BHP watermark in the bottom-left corner."}
    profile = {"overlays": [{"type": "screenshot_ui", "description": "lens button"}, mark]}
    assert has_foreign_watermark(profile)
    assert "Team-BHP watermark in the bottom-left corner)" in watermark_message(profile)
    assert not has_foreign_watermark({"overlays": [{"type": "timestamp"}]})
    assert watermark_message({"overlays": []}) == WATERMARK_MESSAGE


def test_relight_prompt_only_touches_lighting():
    from app.services.prompts import relight_prompt

    p = relight_prompt("dark_luxury")
    assert "Only harmonize lighting" in p and "Do not change the car" in p
    assert "watermark" not in p.lower()  # never asks the model to remove marks
