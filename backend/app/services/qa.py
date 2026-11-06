"""Output QA. Technical and geometry checks are heuristics; thresholds must be calibrated on the
labeled benchmark set (docs/evaluation-plan.md) before launch."""
import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image


@dataclass
class QaResult:
    decision: str                 # approved | retry | review_required | rejected
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reasons": self.reasons, "metrics": self.metrics}


def _load(b: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))


def _edges(rgb: np.ndarray, size: int = 256) -> np.ndarray:
    gray = cv2.cvtColor(cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    e = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 60, 160)
    return cv2.dilate(e, np.ones((5, 5), np.uint8)) > 0


def edge_overlap(src: np.ndarray, out: np.ndarray) -> float:
    """Share of the source's strong edges that still exist in the output (same-angle check).
    Background edges disappear by design, so this is computed against the central region."""
    a, b = _edges(src), _edges(out)
    h, w = a.shape
    roi = (slice(int(h * 0.2), int(h * 0.9)), slice(int(w * 0.1), int(w * 0.9)))
    a, b = a[roi], b[roi]
    if a.sum() == 0:
        return 0.0
    return float((a & b).sum() / a.sum())


def technical_qa(source: bytes, output: bytes, *, pixel_preserving: bool = False,
                 retry_below: float = 0.35, review_below: float = 0.5) -> QaResult:
    try:
        out = _load(output)
    except Exception:
        return QaResult("retry", ["output_not_decodable"])
    src = _load(source)
    reasons, m = [], {}

    oh, ow = out.shape[:2]
    sh, sw = src.shape[:2]
    m["output_size"] = [ow, oh]
    if min(oh, ow) < 512:
        reasons.append("output_too_small")
    ar_diff = abs(ow / oh - sw / sh) / (sw / sh)
    m["aspect_ratio_diff"] = round(ar_diff, 3)
    if ar_diff > 0.15:
        reasons.append("aspect_ratio_changed")

    std = float(out.std())
    m["pixel_std"] = round(std, 1)
    if std < 8:
        return QaResult("retry", ["blank_output"], m)

    if not pixel_preserving:
        resized = cv2.resize(out, (sw, sh)) if (ow, oh) != (sw, sh) else out
        overlap = edge_overlap(src, resized)
        m["edge_overlap"] = round(overlap, 3)
        if overlap < retry_below:
            reasons.append("geometry_changed")
            return QaResult("retry", reasons, m)
        if overlap < review_below:
            reasons.append("geometry_uncertain")

    if "output_too_small" in reasons or "aspect_ratio_changed" in reasons:
        return QaResult("retry", reasons, m)
    if reasons:
        return QaResult("review_required", reasons, m)
    return QaResult("approved", [], m)


def merge_identity(tech: QaResult, identity: dict | None) -> QaResult:
    """Combine technical QA with the optional vision-model identity comparison."""
    if not identity or tech.decision in ("retry", "rejected"):
        return tech
    tech.metrics["identity"] = identity
    verdict = identity.get("verdict")
    if verdict == "different_vehicle":
        return QaResult("retry", tech.reasons + ["identity_mismatch"], tech.metrics)
    if verdict == "uncertain" or identity.get("condition_changed"):
        reasons = tech.reasons + (["condition_changed"] if identity.get("condition_changed") else
                                  ["identity_uncertain"])
        return QaResult("review_required", reasons, tech.metrics)
    return tech
