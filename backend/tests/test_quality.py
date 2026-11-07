"""Image-quality upgrades: detail-preserving re-composite, halo-free matte,
mirrored-plate blur and high-quality JPEG encoding."""
import io

import cv2
import numpy as np
from PIL import Image, JpegImagePlugin

from app.providers.local_composite import matte
from app.services.detail import encode_jpeg, enhance_detail, ensure_jpeg
from app.services.plate import mirrored_boxes
from tests.conftest import to_jpeg


# ---------- detail-preserving re-composite ----------
def _scene(seed=1, h=500, w=750):
    rng = np.random.default_rng(seed)
    bg = cv2.GaussianBlur(rng.integers(60, 200, (h, w, 3)).astype(np.uint8), (0, 0), 6)
    car = np.zeros((h, w), np.uint8)
    cv2.ellipse(car, (w // 2, int(h * 0.58)), (int(w * 0.35), int(h * 0.24)), 0, 0, 360, 255, -1)
    tex = cv2.resize(rng.integers(0, 255, (h // 6, w // 6)).astype(np.uint8), (w, h),
                     interpolation=cv2.INTER_NEAREST).astype(np.float32)
    body = np.stack([tex * 0.5 + 60, tex * 0.3 + 40, tex * 0.2 + 120], -1).astype(np.uint8)
    for i in range(8):
        cv2.putText(body, "STUDIO 42", (int(w * 0.25), int(h * 0.45) + i * 18), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (250, 250, 250), 1)
    return np.where(car[..., None] > 0, body, bg).astype(np.uint8), car


def _soft_studio_output(src, car, scale=1.5):
    m = np.float32([[scale, 0, 25], [0, scale, 12]])
    ow, oh = int(src.shape[1] * scale + 50), int(src.shape[0] * scale + 24)
    gt = cv2.warpAffine(src, m, (ow, oh), flags=cv2.INTER_CUBIC).astype(np.float32) * 0.9 + 15
    mask = cv2.warpAffine(car, m, (ow, oh), flags=cv2.INTER_NEAREST)
    soft = cv2.GaussianBlur(gt, (0, 0), 2.5)
    out = np.where(mask[..., None] > 0, soft, np.full_like(soft, 205))
    return np.clip(out, 0, 255).astype(np.uint8), mask, np.clip(gt, 0, 255).astype(np.uint8)


def test_detail_transfer_restores_real_detail_on_the_car():
    src, car = _scene()
    out, mask, gt = _soft_studio_output(src, car)
    res, m = enhance_detail(to_jpeg(src, quality=95), to_jpeg(out, quality=95), car_mask=mask, src_mask=car)
    assert m["detail"] == "applied" and abs(m["scale"] - 1.5) < 0.02
    assert m["sharpness_after"] > 2 * m["sharpness_before"]
    r = np.asarray(Image.open(io.BytesIO(res)).convert("L")).astype(np.float32)
    g = cv2.cvtColor(gt, cv2.COLOR_RGB2GRAY).astype(np.float32)
    o = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY).astype(np.float32)
    inner = cv2.erode(mask, np.ones((21, 21), np.uint8)) > 0
    assert np.abs(r - g)[inner].mean() < np.abs(o - g)[inner].mean()   # closer to the real car
    bgd = cv2.dilate(mask, np.ones((21, 21), np.uint8)) == 0
    assert np.abs(r - o)[bgd].mean() < 1.5                               # studio background untouched


def test_detail_transfer_skips_unrelated_images():
    src, car = _scene(seed=1)
    other, car2 = _scene(seed=7)
    other = cv2.flip(other, 1)
    res, m = enhance_detail(to_jpeg(src), to_jpeg(other), car_mask=car2, src_mask=car)
    assert m["detail"] == "skipped", m
    assert np.abs(np.asarray(Image.open(io.BytesIO(res))).astype(float) - other.astype(float)).mean() < 3


# ---------- matte / plates / encoding ----------
def test_matte_sits_inside_the_cutout():
    mask = np.zeros((1000, 1500), np.uint8)
    mask[300:700, 400:1100] = 255
    a = matte(mask)
    assert a[500, 398] < 0.02 and a[500, 400] < 0.5      # no bleed beyond the segmentation edge
    assert a[500, 750] > 0.99


def test_mirrored_boxes_reflect_across_floor_line():
    boxes = [[700, 400, 760, 600]]                       # plate just above the floor line at 800
    assert mirrored_boxes(boxes, 800.0) == [[840, 400, 900, 600]]
    assert mirrored_boxes([[100, 0, 150, 50]], 800.0) == []   # far above the floor: no reflection
    assert mirrored_boxes([[820, 0, 860, 50]], 800.0) == []   # already below the line


def test_jpeg_is_high_quality_444():
    rgb = np.random.default_rng(0).integers(0, 255, (64, 64, 3)).astype(np.uint8)
    data = encode_jpeg(rgb)
    im = Image.open(io.BytesIO(data))
    assert JpegImagePlugin.get_sampling(im) == 0
    png = io.BytesIO()
    Image.fromarray(rgb).save(png, "PNG")
    assert ensure_jpeg(png.getvalue())[:3] == b"\xff\xd8\xff"
    assert ensure_jpeg(data) is data


def test_u2net_mask_pre_and_post_processing():
    from app.providers.local_composite import u2net_mask

    seen = {}

    class FakeInput:
        name = "input.1"

    class FakeSession:
        def get_inputs(self):
            return [FakeInput()]

        def run(self, _outputs, feed):
            x = feed["input.1"]
            seen["shape"], seen["dtype"] = x.shape, x.dtype
            pred = np.zeros((1, 1, 320, 320), np.float32)
            pred[0, 0, 100:220, 60:260] = 5.0                       # salient block
            return [pred]

    rgb = np.random.default_rng(0).integers(0, 255, (600, 900, 3)).astype(np.uint8)
    mask = u2net_mask(rgb, FakeSession())
    assert seen["shape"] == (1, 3, 320, 320) and seen["dtype"] == np.float32
    assert mask.shape == (600, 900) and mask.dtype == np.uint8
    assert mask[300, 450] > 200 and mask[20, 20] < 30
