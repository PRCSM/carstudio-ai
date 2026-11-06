import io

import numpy as np
import pytest
from PIL import Image

from app.core.errors import ValidationFailed
from app.services.preprocess import prepare_image, sniff_mime

LIMITS = dict(max_bytes=15 * 1024 * 1024, max_pixels=40_000_000, min_side=512)


def test_sniff_types(car_jpeg):
    assert sniff_mime(car_jpeg) == "image/jpeg"
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    assert sniff_mime(buf.getvalue()) == "image/png"
    assert sniff_mime(b"GIF89a....") is None
    assert sniff_mime(b"%PDF-1.7") is None


def test_valid_image_is_normalized(car_jpeg):
    p = prepare_image(car_jpeg, **LIMITS)
    assert p.mime_type == "image/jpeg" and (p.width, p.height) == (1200, 800)
    assert len(p.sha256) == 64
    assert "blur_variance" in p.quality


def test_large_photos_are_downscaled_to_working_size():
    big = np.random.default_rng(1).integers(0, 255, (3000, 4000, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(big).save(buf, "JPEG", quality=80)
    p = prepare_image(buf.getvalue(), **LIMITS)
    assert max(p.width, p.height) == 2048 and (p.width, p.height) == (2048, 1536)


def test_extension_lies_are_rejected():
    with pytest.raises(ValidationFailed) as e:
        prepare_image(b"<html>not an image</html>", **LIMITS)
    assert e.value.code == "unsupported_type"


def test_too_large_bytes(car_jpeg):
    with pytest.raises(ValidationFailed) as e:
        prepare_image(car_jpeg, max_bytes=1000, max_pixels=40_000_000, min_side=512)
    assert e.value.code == "file_too_large"


def test_decompression_bomb_guard(car_jpeg):
    with pytest.raises(ValidationFailed) as e:
        prepare_image(car_jpeg, max_bytes=LIMITS["max_bytes"], max_pixels=100_000, min_side=10)
    assert e.value.code == "image_too_large"


def test_too_small():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (100, 100, 100)).save(buf, "JPEG")
    with pytest.raises(ValidationFailed) as e:
        prepare_image(buf.getvalue(), **LIMITS)
    assert e.value.code == "image_too_small"


def test_truncated_file(car_jpeg):
    with pytest.raises(ValidationFailed) as e:
        prepare_image(car_jpeg[: len(car_jpeg) // 3], **LIMITS)
    assert e.value.code == "corrupt_image"


def test_exif_orientation_applied_and_stripped():
    img = Image.new("RGB", (1000, 600), (120, 130, 140))
    exif = Image.Exif()
    exif[0x0112] = 6        # rotate 90 CW
    exif[0x8825] = {1: "N"}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    p = prepare_image(buf.getvalue(), **LIMITS)
    assert (p.width, p.height) == (600, 1000)
    out = Image.open(io.BytesIO(p.data))
    assert not out.getexif()


def test_blurry_flagged():
    flat = np.full((800, 1200, 3), 128, np.uint8)
    buf = io.BytesIO()
    Image.fromarray(flat).save(buf, "JPEG")
    assert prepare_image(buf.getvalue(), **LIMITS).quality["is_blurry"]
