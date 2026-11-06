"""Number-plate blur. Boxes come from the vision model in 0-1000 normalised [ymin, xmin, ymax, xmax]."""
import io

import cv2
import numpy as np
from PIL import Image

MAX_PLATE_AREA = 0.08  # a 'plate' covering >8% of the frame is almost certainly a bad detection


def to_pixels(box: list[int], w: int, h: int, pad: float = 0.18) -> tuple[int, int, int, int] | None:
    ymin, xmin, ymax, xmax = (max(0, min(1000, v)) for v in box)
    if ymax <= ymin or xmax <= xmin:
        return None
    bw, bh = xmax - xmin, ymax - ymin
    if (bw * bh) / 1_000_000 > MAX_PLATE_AREA:
        return None
    x0 = int((xmin - bw * pad) * w / 1000)
    x1 = int((xmax + bw * pad) * w / 1000)
    y0 = int((ymin - bh * pad) * h / 1000)
    y1 = int((ymax + bh * pad) * h / 1000)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return x0, y0, x1, y1


def blur_plates(jpeg: bytes, boxes: list[list[int]]) -> tuple[bytes, int]:
    """Smooth, feathered blur over each plate. Returns (image, number of plates blurred)."""
    img = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")).astype(np.float32)
    h, w = img.shape[:2]
    mask = np.zeros((h, w), np.float32)
    count = 0
    for box in boxes:
        px = to_pixels(box, w, h)
        if px is None:
            continue
        x0, y0, x1, y1 = px
        radius = max(2, int(min(x1 - x0, y1 - y0) * 0.25))
        cv2.rectangle(mask, (x0 + radius, y0), (x1 - radius, y1), 1.0, -1)
        cv2.rectangle(mask, (x0, y0 + radius), (x1, y1 - radius), 1.0, -1)
        for cx, cy in ((x0 + radius, y0 + radius), (x1 - radius, y0 + radius),
                       (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)):
            cv2.circle(mask, (cx, cy), radius, 1.0, -1)
        count += 1
    if count == 0:
        return jpeg, 0
    strength = max(15, int(max(h, w) * 0.02)) | 1
    blurred = cv2.GaussianBlur(img, (0, 0), strength)
    blurred = cv2.GaussianBlur(blurred, (0, 0), strength)
    feather = cv2.GaussianBlur(mask, (0, 0), max(2.0, strength * 0.15))[..., None]
    out = img * (1 - feather) + blurred * feather
    from app.services.detail import encode_jpeg
    return encode_jpeg(np.clip(out, 0, 255).astype(np.uint8)), count


def mirrored_boxes(boxes: list[list[int]], axis_y: float) -> list[list[int]]:
    """Boxes reflected across a horizontal floor line (0-1000 scale): where a plate shows in a floor
    reflection. Only plates near the floor line (on the car, not far up the frame) are mirrored."""
    out = []
    for ymin, xmin, ymax, xmax in boxes:
        if ymax > axis_y or axis_y - ymin > 400:
            continue
        out.append([int(2 * axis_y - ymax), xmin, int(2 * axis_y - ymin), xmax])
    return out
