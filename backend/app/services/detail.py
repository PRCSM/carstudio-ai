"""Detail-preserving re-composite for same-angle studio edits.

Image models redraw the whole frame, so fine real detail (badge lettering, grille mesh, wheel spokes, panel
gaps, tyre sidewall text) comes back slightly painted and soft. The customer's photo still has that detail.
This step aligns the source photo onto the AI output and moves the source's fine luminance detail back
onto the car, only where:

  * the two images are geometrically aligned (global similarity + local optical-flow refinement),
  * the local structure agrees (so new studio reflections are not overwritten by the old street ones),
  * the source actually has more fine detail than the output there (sharper AI areas are left alone).

Colour, lighting and background stay exactly as the model made them; only the Y-channel high
frequencies inside the (eroded) car mask change. When alignment is not trustworthy the step is skipped."""
import io
import logging

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

WORK_SIDE = 1200          # feature matching / flow resolution
MAX_OUTPUT_SIDE = 3200    # bigger outputs are left alone (memory on a 1 GB worker)
MIN_INLIERS = 25
MIN_INLIER_RATIO = 0.2


def _load_rgb(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def encode_jpeg(rgb: np.ndarray, quality: int = 95) -> bytes:
    """High-quality JPEG: q95 with no chroma subsampling (4:4:4) keeps edges and colour detail crisp."""
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality, subsampling=0, optimize=True)
    return buf.getvalue()


def ensure_jpeg(image: bytes) -> bytes:
    """Model outputs may be PNG/WebP; deliver everything as high-quality JPEG."""
    if image[:3] == b"\xff\xd8\xff":
        return image
    return encode_jpeg(_load_rgb(image))


def _gray_small(rgb: np.ndarray, side: int) -> tuple[np.ndarray, float]:
    h, w = rgb.shape[:2]
    s = min(1.0, side / max(h, w))
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if s < 1:
        g = cv2.resize(g, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    # local contrast normalisation makes matching robust to the relighting
    g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    return g, s


def _features():
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=4000), cv2.NORM_L2
    return cv2.ORB_create(nfeatures=5000), cv2.NORM_HAMMING


def estimate_alignment(src: np.ndarray, out: np.ndarray) -> tuple[np.ndarray | None, dict]:
    """2x3 similarity transform mapping full-res source pixels to full-res output pixels."""
    gs, ss = _gray_small(src, WORK_SIDE)
    go, so = _gray_small(out, WORK_SIDE)
    det, norm = _features()
    ks, ds = det.detectAndCompute(gs, None)
    ko, do = det.detectAndCompute(go, None)
    info: dict = {"keypoints": [len(ks or []), len(ko or [])]}
    if ds is None or do is None or len(ks) < MIN_INLIERS or len(ko) < MIN_INLIERS:
        return None, {**info, "reason": "too_few_features"}
    matches = cv2.BFMatcher(norm).knnMatch(ds, do, k=2)
    good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.78 * m[1].distance]
    info["matches"] = len(good)
    if len(good) < MIN_INLIERS:
        return None, {**info, "reason": "too_few_matches"}
    p_src = np.float32([ks[m.queryIdx].pt for m in good]) / ss
    p_out = np.float32([ko[m.trainIdx].pt for m in good]) / so
    thresh = max(3.0, 0.004 * max(out.shape[:2]))
    m, inl = cv2.estimateAffinePartial2D(p_src, p_out, method=cv2.RANSAC, ransacReprojThreshold=thresh,
                                         maxIters=4000, confidence=0.995)
    if m is None or inl is None:
        return None, {**info, "reason": "no_transform"}
    n_in = int(inl.sum())
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    rot = float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))
    info.update(inliers=n_in, inlier_ratio=round(n_in / len(good), 3), scale=round(scale, 3),
                rotation_deg=round(rot, 2))
    if n_in < MIN_INLIERS or n_in / len(good) < MIN_INLIER_RATIO:
        return None, {**info, "reason": "weak_alignment"}
    if not 0.25 < scale < 6 or abs(rot) > 6:
        return None, {**info, "reason": "implausible_transform"}
    return m, info


def _refine_flow(warped: np.ndarray, out: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dense optical flow (output -> warped source) to absorb the model's small local redraw shifts.
    Returns the re-warped source and a 0-1 confidence map (low where the needed shift is large)."""
    h, w = out.shape[:2]
    gw, s = _gray_small(warped, 1024)
    go, _ = _gray_small(out, 1024)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow = dis.calc(go, gw, None)
    flow = cv2.GaussianBlur(flow, (0, 0), 2.0)
    flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) / s
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    limit = 0.012 * max(h, w)
    conf = np.clip(1.0 - (mag - limit * 0.5) / (limit * 0.5), 0, 1).astype(np.float32)
    flow *= (mag <= limit)[..., None]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    map_x, map_y = gx + flow[..., 0], gy + flow[..., 1]
    del gx, gy, flow, mag
    rewarped = cv2.remap(warped, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    conf *= cv2.remap(valid, map_x, map_y, cv2.INTER_NEAREST, borderValue=0)
    return rewarped, conf


def _band(y: np.ndarray, sigma: float) -> np.ndarray:
    return y - cv2.GaussianBlur(y, (0, 0), sigma)


def _local_ncc(a: np.ndarray, b: np.ndarray, win: float) -> np.ndarray:
    ma, mb = cv2.GaussianBlur(a, (0, 0), win), cv2.GaussianBlur(b, (0, 0), win)
    va = cv2.GaussianBlur(a * a, (0, 0), win) - ma * ma
    vb = cv2.GaussianBlur(b * b, (0, 0), win) - mb * mb
    cov = cv2.GaussianBlur(a * b, (0, 0), win) - ma * mb
    return cov / np.sqrt(np.maximum(va, 1e-3) * np.maximum(vb, 1e-3))


def sharpness(y: np.ndarray, mask: np.ndarray | None = None) -> float:
    lap = cv2.Laplacian(y.astype(np.float32), cv2.CV_32F)
    return float(lap[mask > 0.5].var()) if mask is not None and (mask > 0.5).any() else float(lap.var())


def enhance_detail(source: bytes, output: bytes, *, strength: float = 0.7,
                   car_mask: np.ndarray | None = None) -> tuple[bytes, dict]:
    """Returns (image, metrics). If the step is skipped the output is returned re-encoded at high quality."""
    src = _load_rgb(source)
    out = _load_rgb(output)
    h, w = out.shape[:2]
    if max(h, w) > MAX_OUTPUT_SIDE:
        return output, {"detail": "skipped", "reason": "output_too_large"}

    m, info = estimate_alignment(src, out)
    if m is None:
        return encode_jpeg(out), {"detail": "skipped", **info}

    warped = cv2.warpAffine(src, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    valid = cv2.warpAffine(np.ones(src.shape[:2], np.float32), m, (w, h), flags=cv2.INTER_NEAREST,
                           borderValue=0)
    del src
    warped, conf = _refine_flow(warped, out, valid)
    del valid

    if car_mask is None:
        from app.providers.local_composite import segment_car
        car_mask, _ = segment_car(out)
    mask = (car_mask.astype(np.float32) / 255.0) if car_mask.dtype == np.uint8 else car_mask.astype(np.float32)
    if mask.shape != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    # stay off the silhouette: the background there is new, so source detail would draw a ghost outline
    erode = max(3, int(0.008 * max(h, w)))
    mask = cv2.erode((mask > 0.5).astype(np.float32), np.ones((erode, erode), np.uint8))
    if mask.mean() < 0.01:
        return encode_jpeg(out), {"detail": "skipped", "reason": "no_car_mask", **info}

    ycc_out = cv2.cvtColor(out, cv2.COLOR_RGB2YCrCb)
    y_out = ycc_out[..., 0].astype(np.float32)
    y_src = cv2.cvtColor(warped, cv2.COLOR_RGB2YCrCb)[..., 0].astype(np.float32)
    del warped
    before = sharpness(y_out, mask)

    # Source detail lives at a coarser scale when the source was smaller than the output.
    upscale = max(1.0, 1.0 / max(float(info["scale"]), 1e-3))
    sigma = max(1.2, 1.1 * upscale)
    hf_src, hf_out = _band(y_src, sigma), _band(y_out, sigma)

    # structural agreement at a mid scale (edges in the same places => same physical feature)
    mid = sigma * 3
    agree = _local_ncc(_band(y_src, mid), _band(y_out, mid), win=max(4.0, 0.012 * max(h, w)))
    agree = np.clip((agree - 0.3) / 0.4, 0, 1)
    del y_src

    # only where the source is locally richer in fine detail than the AI output
    e_src = cv2.GaussianBlur(hf_src * hf_src, (0, 0), sigma * 3)
    e_out = cv2.GaussianBlur(hf_out * hf_out, (0, 0), sigma * 3)
    richer = np.clip((e_src - e_out) / (e_src + 4.0), 0, 1)
    del e_src, e_out

    weight = mask * conf * agree * richer
    weight = cv2.GaussianBlur(weight, (0, 0), max(1.5, sigma))
    delta = np.clip(hf_src - hf_out, -40, 40) * (strength * weight)
    y_new = y_out + delta
    del hf_src, hf_out, delta

    # very light final crispening of the car only (threshold keeps flat paint and noise untouched)
    blur = cv2.GaussianBlur(y_new, (0, 0), 1.0)
    edge = y_new - blur
    y_new += 0.25 * edge * (np.abs(edge) > 2.0) * mask
    after = sharpness(y_new, mask)

    ycc_out[..., 0] = np.clip(y_new, 0, 255).astype(np.uint8)
    result = cv2.cvtColor(ycc_out, cv2.COLOR_YCrCb2RGB)
    metrics = {"detail": "applied", **info, "transfer_coverage": round(float((weight > 0.1).mean()
                                                                           / max(mask.mean(), 1e-6)), 3),
               "sharpness_before": round(before, 1), "sharpness_after": round(after, 1),
               "output_size": [w, h]}
    return encode_jpeg(result), metrics
