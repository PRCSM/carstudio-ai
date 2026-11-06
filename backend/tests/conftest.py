import io

import cv2
import numpy as np
import pytest
from PIL import Image


def _car_scene(w=1200, h=800, seed=0, shift=0) -> np.ndarray:
    """Synthetic 'car on a cluttered street' image: textured background + a car-shaped object."""
    rng = np.random.default_rng(seed)
    bg = rng.integers(60, 200, (h, w, 3), dtype=np.uint8)
    bg = cv2.GaussianBlur(bg, (0, 0), 6)
    for _ in range(25):  # clutter
        x, y = rng.integers(0, w), rng.integers(0, h // 2)
        cv2.rectangle(bg, (int(x), int(y)), (int(x + rng.integers(20, 120)), int(y + rng.integers(20, 160))),
                      tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    cx = w // 2 + shift
    body = np.array([[cx - 380, 560], [cx - 380, 470], [cx - 220, 450], [cx - 120, 360], [cx + 140, 360],
                     [cx + 260, 450], [cx + 380, 470], [cx + 380, 560]], np.int32)
    cv2.fillPoly(bg, [body], (30, 40, 170))
    cv2.fillPoly(bg, [np.array([[cx - 100, 375], [cx + 130, 375], [cx + 220, 450], [cx - 200, 450]], np.int32)],
                 (190, 210, 230))
    for wx in (cx - 230, cx + 230):
        cv2.circle(bg, (wx, 565), 70, (20, 20, 20), -1)
        cv2.circle(bg, (wx, 565), 35, (170, 170, 175), -1)
    cv2.rectangle(bg, (cx + 330, 480), (cx + 375, 505), (240, 240, 200), -1)  # headlight
    return bg


def to_jpeg(rgb: np.ndarray, quality=92) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


@pytest.fixture
def car_jpeg() -> bytes:
    return to_jpeg(_car_scene())


@pytest.fixture
def car_scene():
    return _car_scene
