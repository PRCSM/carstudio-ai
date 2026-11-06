"""Input validation and normalization. Detects type from bytes, guards against
decompression bombs, honors EXIF orientation, strips metadata, and computes quality heuristics.
Heuristics are signals for asking a retake, not proof of anything."""
import hashlib
import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.core.errors import ValidationFailed

try:  # HEIC support is optional
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover
    HEIC_SUPPORTED = False

MAX_WORKING_SIDE = 2048

_SIGNATURES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


def sniff_mime(data: bytes) -> str | None:
    for sig, mime in _SIGNATURES.items():
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "image/heic"
    return None


@dataclass
class PreparedImage:
    data: bytes                  # normalized JPEG, metadata stripped
    mime_type: str
    width: int
    height: int
    sha256: str                  # hash of the original upload bytes (dedupe)
    quality: dict = field(default_factory=dict)


def quality_metrics(img: Image.Image) -> dict:
    gray = np.asarray(img.convert("L").resize((512, max(1, int(512 * img.height / img.width)))))
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    clipped_dark = float((gray < 10).mean())
    clipped_bright = float((gray > 245).mean())
    return {
        "blur_variance": round(blur, 1),
        "is_blurry": blur < 60.0,
        "mean_brightness": round(mean, 1),
        "too_dark": mean < 40 or clipped_dark > 0.5,
        "too_bright": mean > 225 or clipped_bright > 0.5,
    }


def prepare_image(data: bytes, *, max_bytes: int, max_pixels: int, min_side: int) -> PreparedImage:
    if not data:
        raise ValidationFailed("Empty upload.", code="empty_file")
    if len(data) > max_bytes:
        raise ValidationFailed(f"Image is larger than {max_bytes // (1024 * 1024)} MB.", code="file_too_large")

    mime = sniff_mime(data)
    if mime is None:
        raise ValidationFailed("Please send a JPEG, PNG, WebP or HEIC photo.", code="unsupported_type")
    if mime == "image/heic" and not HEIC_SUPPORTED:
        raise ValidationFailed("HEIC photos are not supported yet; please send a JPEG.", code="unsupported_type")

    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(io.BytesIO(data)) as probe:
            w, h = probe.size
            if w * h > max_pixels:
                raise ValidationFailed("Image resolution is too large.", code="image_too_large") from None
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except ValidationFailed:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationFailed("Image resolution is too large.", code="image_too_large") from None
    except Exception:
        raise ValidationFailed("The file could not be read as an image.", code="corrupt_image") from None

    img = ImageOps.exif_transpose(img)  # apply orientation before stripping EXIF
    if min(img.size) < min_side:
        raise ValidationFailed(f"Image is too small; send at least {min_side}px on the short side.",
                               code="image_too_small")

    rgb = img.convert("RGB")  # drops alpha and all metadata on re-encode
    if max(rgb.size) > MAX_WORKING_SIDE:  # bounded working size keeps worker memory predictable
        rgb.thumbnail((MAX_WORKING_SIDE, MAX_WORKING_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=92, optimize=True)
    return PreparedImage(
        data=out.getvalue(),
        mime_type="image/jpeg",
        width=rgb.width,
        height=rgb.height,
        sha256=hashlib.sha256(data).hexdigest(),
        quality=quality_metrics(rgb),
    )
