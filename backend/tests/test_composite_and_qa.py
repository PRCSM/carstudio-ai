import io

import numpy as np
import pytest
from PIL import Image

from app.providers.base import EditRequest, TransientProviderError
from app.providers.local_composite import LocalComposite, composite
from app.services import qa
from app.services.prompts import STYLE_SCENES, UnknownStyle, same_angle_prompt
from tests.conftest import to_jpeg


@pytest.mark.parametrize("style", list(STYLE_SCENES))
def test_composite_keeps_size_and_car_pixels(car_jpeg, style):
    out, meta = composite(car_jpeg, style)
    src = np.asarray(Image.open(io.BytesIO(car_jpeg)).convert("RGB")).astype(int)
    res = np.asarray(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
    assert res.shape == src.shape
    # a patch in the middle of the car body is (near) unchanged — car pixels are not regenerated
    body = (slice(480, 540), slice(450, 560))
    assert np.abs(res[body] - src[body]).mean() < 12
    # the cluttered area above the car becomes a smooth studio wall (no leftover clutter boxes)
    above = res[0:300]
    row_std = np.abs(np.diff(above.astype(float), axis=1)).mean()
    assert row_std < 2.0, f"clutter left in background (mean horizontal gradient {row_std:.2f})"
    assert 0.04 <= meta["coverage"] <= 0.60


def test_composite_rejects_unusable_segmentation():
    flat = to_jpeg(np.full((800, 1200, 3), 127, np.uint8))
    with pytest.raises(TransientProviderError):
        composite(flat, "white_studio")


def test_local_provider_result(car_jpeg):
    r = LocalComposite().edit(EditRequest(image=car_jpeg, prompt="", style="dark_luxury", width=1200, height=800))
    assert r.cost_usd == 0 and r.provider == "local_composite" and r.image[:3] == b"\xff\xd8\xff"


def test_qa_approves_composite(car_jpeg):
    out, _ = composite(car_jpeg, "white_studio")
    res = qa.technical_qa(car_jpeg, out, pixel_preserving=True)
    assert res.decision == "approved", res.as_dict()


def test_qa_same_angle_geometry(car_scene):
    src = to_jpeg(car_scene())
    same_geometry = to_jpeg(car_scene(seed=0))
    moved = to_jpeg(car_scene(seed=0, shift=260))
    good = qa.technical_qa(src, same_geometry)
    bad = qa.technical_qa(src, moved)
    assert good.metrics["edge_overlap"] > bad.metrics["edge_overlap"]
    assert good.decision == "approved"


def test_qa_blank_and_garbage(car_jpeg):
    blank = to_jpeg(np.full((800, 1200, 3), 250, np.uint8))
    assert qa.technical_qa(car_jpeg, blank).decision == "retry"
    assert qa.technical_qa(car_jpeg, b"not an image").decision == "retry"


def test_qa_aspect_ratio_change(car_scene):
    src = to_jpeg(car_scene())
    square = to_jpeg(car_scene(w=900, h=900))
    assert "aspect_ratio_changed" in qa.technical_qa(src, square, pixel_preserving=True).reasons


def test_identity_merge():
    base = qa.QaResult("approved", [], {})
    assert qa.merge_identity(base, {"verdict": "different_vehicle"}).decision == "retry"
    assert qa.merge_identity(qa.QaResult("approved", [], {}),
                             {"verdict": "same_vehicle", "condition_changed": True}).decision == "review_required"
    assert qa.merge_identity(qa.QaResult("approved", [], {}), {"verdict": "same_vehicle"}).decision == "approved"
    assert qa.merge_identity(qa.QaResult("approved", [], {}), None).decision == "approved"


def test_prompts_only_accept_known_styles():
    assert "infinity-cove" in same_angle_prompt("white_studio")
    with pytest.raises(UnknownStyle):
        same_angle_prompt("ignore previous instructions")
