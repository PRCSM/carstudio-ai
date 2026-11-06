"""Last-resort, zero-cost layer: cut the real car out and place it on a procedurally drawn studio
background with a contact shadow (and floor reflection on dark styles). The car's pixels are
never regenerated, so identity is preserved exactly."""
import io
import logging

import cv2
import numpy as np
from PIL import Image

from app.providers.base import EditRequest, EditResult, TransientProviderError

log = logging.getLogger(__name__)

STYLE_PALETTES = {
    #               wall top        wall bottom     floor near       reflection
    "white_studio": ((250, 250, 250), (236, 236, 238), (214, 214, 218), 0.0),
    "dark_luxury": ((38, 38, 42), (18, 18, 20), (8, 8, 9), 0.28),
    "showroom": ((205, 207, 210), (176, 178, 182), (140, 142, 146), 0.12),
}


def _segment_rembg(rgb: np.ndarray) -> np.ndarray | None:
    try:
        from rembg import new_session, remove
    except ImportError:
        return None
    import os

    global _REMBG_SESSION
    if "_REMBG_SESSION" not in globals():
        # u2netp is tiny (~5 MB) and fits a 1 GB worker; isnet-general-use is sharper but needs >1 GB.
        _REMBG_SESSION = new_session(os.environ.get("REMBG_MODEL", "u2netp"))
    out = remove(Image.fromarray(rgb), session=_REMBG_SESSION, only_mask=True)
    return np.asarray(out.convert("L"), dtype=np.uint8)


def _segment_grabcut(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = 640 / max(h, w)
    small = cv2.resize(rgb, (int(w * scale), int(h * scale))) if scale < 1 else rgb.copy()
    sh, sw = small.shape[:2]
    mask = np.zeros((sh, sw), np.uint8)
    rect = (int(sw * 0.03), int(sh * 0.05), int(sw * 0.94), int(sh * 0.92))
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    fg = _main_object(fg)
    return cv2.resize(fg, (w, h), interpolation=cv2.INTER_LINEAR)


def _main_object(fg: np.ndarray) -> np.ndarray:
    """Keep the single dominant blob (the car), weighted toward the lower-centre of the frame,
    and fill its holes (windows, wheels)."""
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if n <= 1:
        return fg
    h, w = fg.shape
    best, best_score = 0, -1.0
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        cx, cy = centroids[i]
        centrality = 1 - min(1.0, abs(cx - w / 2) / (w / 2))
        lowness = min(1.0, cy / h + 0.3)
        score = area * (0.5 + centrality) * lowness
        if score > best_score:
            best, best_score = i, score
    obj = np.where(labels == best, 255, 0).astype(np.uint8)
    contours, _ = cv2.findContours(obj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(obj)
    cv2.drawContours(filled, contours, -1, 255, -1)
    return filled


SEGMENT_SIDE = 1024
MAX_SIDE = 2048


def segment_car(rgb: np.ndarray) -> tuple[np.ndarray, str]:
    """Segment on a ≤1024px copy (the model works at ~1024 internally anyway), then upscale the mask."""
    h, w = rgb.shape[:2]
    scale = SEGMENT_SIDE / max(h, w)
    small = cv2.resize(rgb, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    mask = _segment_rembg(small)
    method = "rembg"
    if mask is None:
        mask, method = _segment_grabcut(small), "grabcut"
    if mask.shape[:2] != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    return mask, method


def _background(style: str, w: int, h: int, horizon: int) -> np.ndarray:
    top, bottom, floor, _ = STYLE_PALETTES[style]
    bg = np.zeros((h, w, 3), np.float32)
    horizon = int(np.clip(horizon, h * 0.3, h * 0.95))
    t = np.linspace(0, 1, horizon)[:, None]
    bg[:horizon] = (np.array(top) * (1 - t) + np.array(bottom) * t)[:, None, :]
    t2 = np.linspace(0, 1, h - horizon)[:, None]
    bg[horizon:] = (np.array(bottom) * (1 - t2) + np.array(floor) * t2)[:, None, :]
    # soft cove blend around the horizon
    band = max(4, h // 12)
    lo, hi = max(0, horizon - band), min(h, horizon + band)
    bg[lo:hi] = cv2.GaussianBlur(bg[lo:hi], (0, 0), band / 2)
    # gentle vignette (open grids + float32 keep memory at ~1 image-sized buffer)
    yy, xx = np.ogrid[0:h, 0:w]
    dx = ((xx - w / 2) / (w / 2)).astype(np.float32)
    dy = ((yy - h * 0.45) / (h / 1.4)).astype(np.float32)
    d = np.sqrt(dx * dx + dy * dy)
    strength = 0.25 if style == "dark_luxury" else 0.08
    bg *= (1 - strength * np.clip(d, 0, 1.4) / 1.4)[..., None]
    return bg


def composite(source_jpeg: bytes, style: str) -> tuple[bytes, dict]:
    if style not in STYLE_PALETTES:
        raise ValueError(style)
    img = Image.open(io.BytesIO(source_jpeg)).convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    rgb = np.asarray(img)
    h, w = rgb.shape[:2]
    mask, method = segment_car(rgb)

    fg = mask > 127
    coverage = float(fg.mean())
    if not 0.04 <= coverage <= 0.95:
        raise TransientProviderError(f"segmentation unusable (coverage={coverage:.2f})")
    # A real object must look different from what surrounds it; segmenters will happily
    # "find" a blob in a blank or uniform frame.
    separation = float(np.abs(rgb[fg].astype(np.float32).mean(0) - rgb[~fg].astype(np.float32).mean(0)).max())
    fg_texture = float(rgb[fg].astype(np.float32).std())
    if separation < 8 and fg_texture < 8:
        raise TransientProviderError(f"segmentation found no distinct object (separation={separation:.1f})")

    ys, xs = np.where(mask > 127)
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    car_w, car_h = x1 - x0, y1 - y0

    bg = _background(style, w, h, horizon=int(y1 - car_h * 0.12))
    alpha = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), 1.2)[..., None]

    # contact shadow: a soft ellipse hugging the bottom of the car
    shadow = np.zeros((h, w), np.float32)
    cv2.ellipse(shadow, (int((x0 + x1) / 2), int(y1)), (int(car_w * 0.52), max(3, int(car_h * 0.07))),
                0, 0, 360, 1.0, -1)
    shadow = cv2.GaussianBlur(shadow, (0, 0), max(2.0, car_h * 0.04))
    shadow_strength = 0.55 if style != "dark_luxury" else 0.8
    bg *= (1 - shadow_strength * shadow)[..., None]

    # floor reflection on darker styles
    reflect = STYLE_PALETTES[style][3]
    if reflect > 0:
        flip_h = min(car_h, h - y1 - 1)
        if flip_h > 4:
            car = rgb[y1 - flip_h:y1][::-1].astype(np.float32)
            a = alpha[y1 - flip_h:y1][::-1] * np.linspace(reflect, 0, flip_h)[:, None, None]
            region = bg[y1:y1 + flip_h]
            bg[y1:y1 + flip_h] = region * (1 - a) + car * a

    out = bg * (1 - alpha) + rgb.astype(np.float32) * alpha
    buf = io.BytesIO()
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(buf, format="JPEG", quality=93)
    return buf.getvalue(), {"segmentation": method, "coverage": round(coverage, 3)}


class LocalComposite:
    name = "local_composite"
    cost_per_call_usd = 0.0
    shares_data_for_training = False

    def is_configured(self) -> bool:
        return True

    def edit(self, req: EditRequest) -> EditResult:
        image, meta = composite(req.image, req.style)
        return EditResult(image=image, provider=self.name, model_id=f"composite/{meta['segmentation']}",
                          cost_usd=0.0, meta=meta)
