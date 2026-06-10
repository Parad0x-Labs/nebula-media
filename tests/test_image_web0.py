# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Parad0x Labs
"""Tests for nebula.web0 image path — AVIF/WebP encode, alpha, guards."""

import hashlib
import random

import pytest
from PIL import Image, ImageDraw, features

from nebula.web0 import ContentType, encode_image_web0

AVIF = features.check("avif")
needs_avif = pytest.mark.skipif(not AVIF, reason="Pillow built without AVIF (needs pillow>=11.3)")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_photo(path, size=(512, 384), seed=7):
    """Noisy gradient — photo-like, compresses well but not trivially."""
    rng = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            n = rng.randint(-12, 12)
            px[x, y] = (
                max(0, min(255, (x * 255) // w + n)),
                max(0, min(255, (y * 255) // h + n)),
                max(0, min(255, ((x + y) * 255) // (w + h) + n)),
            )
    img.save(path)
    return path


def make_alpha_logo(path, size=(256, 256)):
    """Logo with a real alpha gradient (not just binary transparency)."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size[0] // 2
    for i in range(c - 6, 0, -2):
        d.ellipse([c - i, c - i, c + i, c + i], fill=(20, 240, 150, max(0, 255 - i)))
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

@needs_avif
def test_basic_avif_compresses_and_reports(tmp_path):
    src = make_photo(tmp_path / "photo.png")
    r = encode_image_web0(src)

    assert r.encoder == "avif"
    assert r.output_path.suffix == ".avif"
    assert r.output_path.exists()
    assert r.output_size < r.source_size
    assert r.ratio > 1.0
    assert 0.9 < r.quality_score <= 1.0
    assert r.quality_setting > 0
    assert len(r.proof_hash) == 64
    assert r.proof_hash == hashlib.sha256(r.output_path.read_bytes()).hexdigest()
    assert r.arweave_cost_usd_at_30 < r.arweave_cost_usd_at_30 + r.arweave_savings_usd_at_30


@needs_avif
def test_alpha_preserved(tmp_path):
    src = make_alpha_logo(tmp_path / "logo.png")
    r = encode_image_web0(src, keep_original_if_larger=False)

    assert r.encoder == "avif"
    out = Image.open(r.output_path)
    out.load()
    assert out.mode == "RGBA"
    px = out.getpixel((1, 1))
    assert px[3] == 0, "fully transparent corner must stay transparent"
    mid_alpha = out.getpixel((out.size[0] // 2, out.size[1] // 2 - 40))[3]
    assert 0 < mid_alpha <= 255


@needs_avif
def test_grow_guard_keeps_original(tmp_path):
    src = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8), (255, 255, 255)).save(src, optimize=True)
    r = encode_image_web0(src)

    assert r.encoder == "copy"
    assert r.output_path.suffix == ".png", "kept bytes must keep their own extension"
    assert r.output_size == r.source_size
    assert r.quality_score == 1.0
    assert "original kept" in r.note
    assert not list(tmp_path.glob("*.avif")), "failed attempt must be cleaned up"


@needs_avif
def test_grow_guard_can_be_disabled(tmp_path):
    src = tmp_path / "tiny.png"
    Image.new("RGB", (8, 8), (255, 255, 255)).save(src, optimize=True)
    r = encode_image_web0(src, keep_original_if_larger=False)

    assert r.encoder == "avif"
    assert r.output_path.suffix == ".avif"


def test_animated_gif_refused(tmp_path):
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (64, 64), c) for c in [(255, 0, 0), (0, 255, 0)]]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=80)

    with pytest.raises(ValueError, match="animated"):
        encode_image_web0(src, fmt="webp")


@needs_avif
def test_explicit_quality_disables_retry(tmp_path):
    src = make_photo(tmp_path / "photo.png")
    r = encode_image_web0(src, quality=30)
    assert r.quality_setting == 30, "explicit quality must be honoured (no SSIM retry)"


@needs_avif
def test_target_ssim_retries_at_higher_quality(tmp_path):
    src = make_photo(tmp_path / "photo.png")
    # Unreachable floor forces both retries: 10 → 18 → 26.
    r = encode_image_web0(src, quality=10, target_ssim=0.9999, max_quality_retries=2)
    assert r.quality_setting == 26


def test_webp_path(tmp_path):
    src = make_photo(tmp_path / "photo.png")
    r = encode_image_web0(src, fmt="webp")
    assert r.encoder in ("webp", "copy")
    if r.encoder == "webp":
        assert r.output_path.suffix == ".webp"
        assert Image.open(r.output_path).format == "WEBP"


@needs_avif
def test_exif_orientation_applied(tmp_path):
    src = tmp_path / "rotated.jpg"
    img = Image.new("RGB", (100, 50), (200, 30, 30))
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90 CW on display
    img.save(src, exif=exif, quality=90)

    r = encode_image_web0(src, keep_original_if_larger=False)
    out = Image.open(r.output_path)
    assert out.size == (50, 100), "EXIF orientation must be baked into the output"


@needs_avif
def test_content_type_override(tmp_path):
    src = make_photo(tmp_path / "photo.png")  # .png auto-detects as screenshot
    r_auto = encode_image_web0(src, output=tmp_path / "a.avif")
    r_photo = encode_image_web0(src, output=tmp_path / "b.avif",
                                content_type=ContentType.PHOTO)
    assert r_auto.content_type == ContentType.SCREENSHOT
    assert r_photo.content_type == ContentType.PHOTO
    assert r_photo.quality_setting < r_auto.quality_setting
