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

WORK_SIDE = 800           # feature matching resolution (SIFT doubles it internally: ~80 MB here)
MAX_OUTPUT_SIDE = 3200    # bigger outputs are left alone (memory on a 1 GB worker)
MIN_SCORE = 0.45          # edge-structure correlation needed to trust an alignment


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
        return cv2.SIFT_create(nfeatures=3000), cv2.NORM_L2
    return cv2.ORB_create(nfeatures=4000), cv2.NORM_HAMMING


def _small_mask(mask: np.ndarray | None, shape: tuple[int, int], dilate: int = 0) -> np.ndarray | None:
    if mask is None:
        return None
    m = cv2.resize((mask > 127).astype(np.uint8) * 255, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    if dilate:
        m = cv2.dilate(m, np.ones((dilate, dilate), np.uint8))
    return m


def _grad(g: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.GaussianBlur(cv2.magnitude(gx, gy), (0, 0), 1.0)


def alignment_score(gs: np.ndarray, go: np.ndarray, m_small: np.ndarray, mask_small: np.ndarray | None) -> float:
    """Correlation of edge structure between the warped source and the output, inside the car."""
    warped = cv2.warpAffine(_grad(gs), m_small, (go.shape[1], go.shape[0]), flags=cv2.INTER_LINEAR)
    valid = cv2.warpAffine(np.ones_like(gs, np.uint8), m_small, (go.shape[1], go.shape[0]),
                           flags=cv2.INTER_NEAREST) > 0
    go_g = _grad(go)
    sel = valid if mask_small is None else (valid & (mask_small > 0))
    if sel.sum() < 500:
        return 0.0
    a, b = warped[sel], go_g[sel]
    a, b = a - a.mean(), b - b.mean()
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6))


def _feature_alignment(gs, go, ms, mo, info) -> np.ndarray | None:
    det, norm = _features()
    ks, ds = det.detectAndCompute(gs, ms)
    ko, do = det.detectAndCompute(go, mo)
    info["keypoints"] = [len(ks or []), len(ko or [])]
    if ds is None or do is None or len(ks) < 8 or len(ko) < 8:
        return None
    matches = cv2.BFMatcher(norm).knnMatch(ds, do, k=2)
    good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.82 * m[1].distance]
    info["matches"] = len(good)
    if len(good) < 8:
        return None
    p_src = np.float32([ks[m.queryIdx].pt for m in good])
    p_out = np.float32([ko[m.trainIdx].pt for m in good])
    m, inl = cv2.estimateAffinePartial2D(p_src, p_out, method=cv2.RANSAC,
                                         ransacReprojThreshold=max(3.0, 0.006 * max(go.shape)),
                                         maxIters=5000, confidence=0.995)
    if m is None or inl is None:
        return None
    info.update(inliers=int(inl.sum()), inlier_ratio=round(float(inl.sum()) / len(good), 3))
    return m if int(inl.sum()) >= 8 else None


def _mask_alignment(ms, mo) -> np.ndarray | None:
    """Similarity from the two car silhouettes (area -> scale, centroid -> shift)."""
    if ms is None or mo is None:
        return None
    a, b = cv2.moments(ms, binaryImage=True), cv2.moments(mo, binaryImage=True)
    if a["m00"] < 100 or b["m00"] < 100:
        return None
    scale = float(np.sqrt(b["m00"] / a["m00"]))
    cs = np.array([a["m10"] / a["m00"], a["m01"] / a["m00"]])
    co = np.array([b["m10"] / b["m00"], b["m01"] / b["m00"]])
    t = co - scale * cs
    return np.array([[scale, 0, t[0]], [0, scale, t[1]]], np.float64)


def _ecc_refine(gs, go, m_small, mo) -> np.ndarray | None:
    """Refine src->out (small coords) by ECC on edge maps, which ignores the relighting."""
    try:
        inv = cv2.invertAffineTransform(m_small).astype(np.float32)  # ECC estimates out->src
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
        _, w = cv2.findTransformECC(_grad(go), _grad(gs), inv, cv2.MOTION_AFFINE, crit, mo, 5)
        return cv2.invertAffineTransform(w).astype(np.float64)
    except cv2.error:
        return None


def estimate_alignment(src: np.ndarray, out: np.ndarray, src_mask: np.ndarray | None = None,
                       out_mask: np.ndarray | None = None) -> tuple[np.ndarray | None, dict]:
    """2x3 transform mapping full-res source pixels to full-res output pixels. Candidates come from
    car-restricted feature matching and from the silhouettes (+ECC); the best-scoring one is kept,
    and only if the edges really line up."""
    gs, ss = _gray_small(src, WORK_SIDE)
    go, so = _gray_small(out, WORK_SIDE)
    ms = _small_mask(src_mask, gs.shape, dilate=9)
    mo = _small_mask(out_mask, go.shape, dilate=9)
    mo_score = _small_mask(out_mask, go.shape)
    info: dict = {}
    candidates: dict[str, np.ndarray] = {}
    m = _feature_alignment(gs, go, ms, mo, info)
    if m is not None:
        candidates["features"] = m
        r = _ecc_refine(gs, go, m, mo)
        if r is not None:
            candidates["features+ecc"] = r
    m = _mask_alignment(_small_mask(src_mask, gs.shape), mo_score)
    if m is not None:
        r = _ecc_refine(gs, go, m, mo)
        if r is not None:
            candidates["silhouette+ecc"] = r
    if not candidates:
        return None, {**info, "reason": "no_alignment"}
    scores = {k: alignment_score(gs, go, v, mo_score) for k, v in candidates.items()}
    best = max(scores, key=scores.get)
    m_small = candidates[best]
    # back to full-res coordinates
    m_full = m_small.copy()
    m_full[:, :2] *= ss / so
    m_full[:, 2] /= so
    scale = float(np.sqrt(abs(np.linalg.det(m_full[:, :2]))))
    rot = float(np.degrees(np.arctan2(m_full[1, 0], m_full[0, 0])))
    info.update(method=best, score=round(scores[best], 3), scale=round(scale, 3), rotation_deg=round(rot, 2))
    if scores[best] < MIN_SCORE:
        return None, {**info, "reason": "weak_alignment"}
    if not 0.25 < scale < 6 or abs(rot) > 6:
        return None, {**info, "reason": "implausible_transform"}
    return m_full, info


def _refine_flow(warped: np.ndarray, out: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dense optical flow (output -> warped source) to absorb the model's small local redraw shifts.
    Returns the re-warped source and a 0-1 confidence map (low where the needed shift is large).
    Flow, magnitude and confidence are computed at <=1024 px; only the two remap grids are full size."""
    h, w = out.shape[:2]
    gw, s = _gray_small(warped, 1024)
    go, _ = _gray_small(out, 1024)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow = cv2.GaussianBlur(dis.calc(go, gw, None), (0, 0), 2.0)
    mag = cv2.magnitude(flow[..., 0], flow[..., 1]) / s          # in full-res pixels
    limit = 0.02 * max(h, w)
    conf_s = np.clip(1.0 - (mag - limit * 0.6) / (limit * 0.4), 0, 1).astype(np.float32)
    flow *= (mag <= limit)[..., None]
    map_x = cv2.resize(flow[..., 0], (w, h), interpolation=cv2.INTER_LINEAR)
    map_x /= s
    map_x += np.arange(w, dtype=np.float32)[None, :]
    map_y = cv2.resize(flow[..., 1], (w, h), interpolation=cv2.INTER_LINEAR)
    map_y /= s
    map_y += np.arange(h, dtype=np.float32)[:, None]
    del flow, mag
    rewarped = cv2.remap(warped, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    valid_r = cv2.remap(valid, map_x, map_y, cv2.INTER_NEAREST, borderValue=0)
    del map_x, map_y
    conf = cv2.resize(conf_s, (w, h), interpolation=cv2.INTER_LINEAR)
    conf *= valid_r
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


BYTES_PER_PIXEL = 60  # measured RSS growth of the full-res stage (~45 B/px), with margin


def memory_headroom_mb() -> float | None:
    """Memory still available under this container's cgroup limit, or None when it can't be read.
    Reclaimable page cache (inactive files) is not counted as used."""
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            limit = f.read().strip()
        if limit == "max":
            return None
        with open("/sys/fs/cgroup/memory.current") as f:
            used = int(f.read().strip())
        with open("/sys/fs/cgroup/memory.stat") as f:
            stat = dict(line.split() for line in f if line.strip())
        used -= int(stat.get("inactive_file", 0))
        return (int(limit) - used) / 1_048_576
    except (OSError, ValueError):
        return None


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return -1.0


def log_mem(stage: str) -> None:
    free = memory_headroom_mb()
    log.info("mem %s: rss=%.0fMB headroom=%s", stage, _rss_mb(), "n/a" if free is None else f"{free:.0f}MB")


def _segment(rgb: np.ndarray) -> np.ndarray:
    """Rough car mask for alignment and gating. GrabCut at 640 px is enough here (edges are eroded and
    every pixel is gated by structural agreement) and costs a few MB; rembg on a 2K frame took ~700 MB
    and got the 1 GB worker OOM-killed."""
    from app.providers.local_composite import _segment_grabcut
    return _segment_grabcut(rgb)


def enhance_detail(source: bytes, output: bytes, *, strength: float = 0.85,
                   car_mask: np.ndarray | None = None, src_mask: np.ndarray | None = None) -> tuple[bytes, dict]:
    """Returns (image, metrics). If the step is skipped the output is returned re-encoded at high quality.
    car_mask / src_mask (uint8 0-255) are computed with the segmenter when not given."""
    src = _load_rgb(source)
    out = _load_rgb(output)
    h, w = out.shape[:2]
    if max(h, w) > MAX_OUTPUT_SIDE:
        return output, {"detail": "skipped", "reason": "output_too_large"}
    log_mem("detail:start")
    if car_mask is None:
        car_mask = _segment(out)
        log_mem("detail:segmented_output")
    if src_mask is None:
        src_mask = _segment(src)
        log_mem("detail:segmented_source")
    if car_mask.shape != (h, w):
        car_mask = cv2.resize(car_mask, (w, h), interpolation=cv2.INTER_NEAREST)

    m, info = estimate_alignment(src, out, src_mask, car_mask)
    log_mem("detail:aligned")
    if m is None:
        return encode_jpeg(out), {"detail": "skipped", **info}

    # Full-resolution work happens only inside the car's bounding box.
    ys_, xs_ = np.nonzero(car_mask > 127)
    pad = int(0.03 * max(h, w))
    x0, x1 = max(0, int(xs_.min()) - pad), min(w, int(xs_.max()) + pad + 1)
    y0, y1 = max(0, int(ys_.min()) - pad), min(h, int(ys_.max()) + pad + 1)
    del ys_, xs_
    need = (x1 - x0) * (y1 - y0) * BYTES_PER_PIXEL / 1_048_576 + 80
    free = memory_headroom_mb()
    if free is not None and free < need:
        return encode_jpeg(out), {"detail": "skipped", "reason": "low_memory", "free_mb": round(free),
                                  "need_mb": round(need), **info}
    full_out, (fh, fw) = out, (h, w)
    out = np.ascontiguousarray(out[y0:y1, x0:x1])
    car_mask = car_mask[y0:y1, x0:x1]
    h, w = out.shape[:2]
    mc = m.copy()
    mc[:, 2] -= (x0, y0)

    warped = cv2.warpAffine(src, mc, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    valid = cv2.warpAffine(np.ones(src.shape[:2], np.uint8), mc, (w, h), flags=cv2.INTER_NEAREST,
                           borderValue=0)
    del src
    warped, conf = _refine_flow(warped, out, valid)
    del valid
    log_mem("detail:flow")

    # stay off the silhouette: the background there is new, so source detail would draw a ghost outline
    erode = max(3, int(0.008 * max(h, w)))
    mask = cv2.erode((car_mask > 127).astype(np.uint8), np.ones((erode, erode), np.uint8)).astype(np.float32)
    if mask.mean() < 0.01:
        return encode_jpeg(full_out), {"detail": "skipped", "reason": "no_car_mask", **info}

    ycc_out = cv2.cvtColor(out, cv2.COLOR_RGB2YCrCb)
    del out
    y_out = ycc_out[..., 0].astype(np.float32)
    y_src = cv2.cvtColor(warped, cv2.COLOR_RGB2YCrCb)[..., 0].astype(np.float32)
    del warped
    before = sharpness(y_out, mask)

    # Source detail lives at a coarser scale when the source was smaller than the output.
    upscale = max(1.0, 1.0 / max(float(info["scale"]), 1e-3))
    sigma = max(1.2, 1.1 * upscale)
    hf_src, hf_out = _band(y_src, sigma), _band(y_out, sigma)

    # Gating maps are smooth, so compute them at reduced resolution (memory on a 1 GB worker).
    r = min(1.0, 1400 / max(h, w))
    small = (max(1, int(w * r)), max(1, int(h * r)))

    def down(a: np.ndarray) -> np.ndarray:
        return cv2.resize(a, small, interpolation=cv2.INTER_AREA) if r < 1 else a

    ys, yo = down(y_src), down(y_out)
    del y_src
    mid = max(1.5, sigma * 3 * r)
    # structural agreement at a mid scale (edges in the same places => same physical feature)
    agree = _local_ncc(_band(ys, mid), _band(yo, mid), win=max(4.0, 0.012 * max(small)))
    agree = np.clip((agree - 0.15) / 0.35, 0, 1)
    # only where the source is locally richer in fine detail than the AI output
    hs, ho = down(hf_src), down(hf_out)
    e_src = cv2.GaussianBlur(hs * hs, (0, 0), max(1.0, sigma * 3 * r))
    e_out = cv2.GaussianBlur(ho * ho, (0, 0), max(1.0, sigma * 3 * r))
    richer = np.clip((e_src - e_out) / (e_src + 4.0), 0, 1)
    cs = down(conf)
    msk = down(mask) > 0.5
    gates = {k: round(float(v[msk].mean()), 3) if msk.any() else 0.0
             for k, v in (("aligned", cs), ("agree", agree), ("richer", richer))}
    gate = cv2.GaussianBlur(cs * agree * richer, (0, 0), max(1.0, sigma * r))
    del ys, yo, agree, hs, ho, e_src, e_out, richer, conf, cs
    weight = (cv2.resize(gate, (w, h), interpolation=cv2.INTER_LINEAR) if r < 1 else gate) * mask
    weight = cv2.GaussianBlur(weight, (0, 0), 1.0)

    y_new = y_out + np.clip(hf_src - hf_out, -40, 40) * (strength * weight)
    del hf_src, hf_out, y_out

    # very light final crispening of the car only (threshold keeps flat paint and noise untouched)
    edge = y_new - cv2.GaussianBlur(y_new, (0, 0), 1.0)
    y_new += 0.25 * edge * (np.abs(edge) > 2.0) * mask
    del edge
    after = sharpness(y_new, mask)
    log_mem("detail:transferred")

    ycc_out[..., 0] = np.clip(y_new, 0, 255).astype(np.uint8)
    result = full_out.copy()
    result[y0:y1, x0:x1] = cv2.cvtColor(ycc_out, cv2.COLOR_YCrCb2RGB)
    metrics = {"detail": "applied", **info,
               "transfer_coverage": round(float((weight > 0.1).mean() / max(mask.mean(), 1e-6)), 3),
               "gates": gates, "sharpness_before": round(before, 1), "sharpness_after": round(after, 1),
               "output_size": [fw, fh], "region": [x0, y0, x1, y1]}
    return encode_jpeg(result), metrics
