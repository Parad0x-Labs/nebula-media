# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Parad0x Labs
"""Tests for nebula.page — .null site folder compression + reference rewriting."""

import hashlib
import json

import pytest
from PIL import Image, features

from nebula.page import compress_page

from .test_image_web0 import make_alpha_logo, make_photo

AVIF = features.check("avif")
needs_avif = pytest.mark.skipif(not AVIF, reason="Pillow built without AVIF (needs pillow>=11.3)")


# ---------------------------------------------------------------------------
# Site fixture
# ---------------------------------------------------------------------------

def make_site(root):
    """A small but structurally nasty static site."""
    (root / "img").mkdir(parents=True)
    (root / "blog" / "img").mkdir(parents=True)
    (root / "css").mkdir()

    make_photo(root / "img" / "hero.png", size=(480, 320), seed=3)
    make_alpha_logo(root / "img" / "logo.png")
    # Duplicate basename: the big one converts, the small one is skipped
    # (under min_bytes) — bare "banner.png" references must NOT be rewritten.
    make_photo(root / "img" / "banner.png", size=(400, 120), seed=5)
    Image.new("RGB", (24, 8), (9, 9, 9)).save(root / "blog" / "img" / "banner.png")
    # Animated GIF — must be left alone.
    frames = [Image.new("RGB", (48, 48), c) for c in [(255, 0, 0), (0, 0, 255)]]
    frames[0].save(root / "img" / "anim.gif", save_all=True,
                   append_images=frames[1:], duration=90)
    # Tiny icon — skipped under min_bytes.
    Image.new("RGB", (16, 16), (250, 250, 250)).save(root / "img" / "dot.png")

    (root / "index.html").write_text(
        "<html><head><link rel=stylesheet href=css/style.css>"
        "<meta property=og:image content=/img/hero.png></head><body>"
        "<img src=img/hero.png srcset='img/hero.png 1x, ./img/hero.png 2x'>"
        "<img src='img/logo.png'><img src=img/banner.png>"
        "<img src=img/anim.gif><img src=img/dot.png>"
        "</body></html>",
        encoding="utf-8",
    )
    (root / "blog" / "post.html").write_text(
        "<html><body><img src=../img/hero.png><img src=img/banner.png>"
        "<img src=../img/logo.png></body></html>",
        encoding="utf-8",
    )
    (root / "css" / "style.css").write_text(
        "body{background:url(../img/hero.png)}"
        ".l{background-image:url('../img/logo.png')}",
        encoding="utf-8",
    )
    (root / "app.js").write_text(
        "const imgs=['hero.png','logo.png'];const p='img/'+imgs[0];",
        encoding="utf-8",
    )
    return root


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@needs_avif
def test_page_end_to_end(tmp_path):
    site = make_site(tmp_path / "site")
    before = tree_digest(site)

    r = compress_page(site)

    # Source untouched.
    assert tree_digest(site) == before
    out = r.output_dir
    assert out == tmp_path / "site_web0"

    # hero + logo + big banner converted; tiny/dup-banner/gif/dot kept.
    assert r.images_converted == 3
    assert (out / "img" / "hero.avif").exists()
    assert (out / "img" / "logo.avif").exists()
    assert (out / "img" / "banner.avif").exists()
    assert not (out / "img" / "hero.png").exists()
    assert (out / "blog" / "img" / "banner.png").exists()
    assert (out / "img" / "anim.gif").exists()
    assert (out / "img" / "dot.png").exists()

    # Alpha survived the trip.
    logo = Image.open(out / "img" / "logo.avif")
    logo.load()
    assert logo.mode == "RGBA"
    assert logo.getpixel((1, 1))[3] == 0

    # References rewritten — every form.
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "img/hero.avif" in index and "img/hero.png" not in index
    assert "./img/hero.avif 2x" in index
    assert "/img/hero.avif" in index            # og:image absolute form
    assert "img/logo.avif" in index
    assert "img/banner.avif" in index           # root index refers to the BIG banner
    assert "img/anim.gif" in index and "img/dot.png" in index

    post = (out / "blog" / "post.html").read_text(encoding="utf-8")
    assert "../img/hero.avif" in post and "../img/logo.avif" in post
    # blog-relative img/banner.png points at the SKIPPED small banner — must stay.
    assert "img/banner.png" in post

    css = (out / "css" / "style.css").read_text(encoding="utf-8")
    assert "url(../img/hero.avif)" in css and "url('../img/logo.avif')" in css

    js = (out / "app.js").read_text(encoding="utf-8")
    # 'logo.png' basename is unique in the tree → bare rewrite allowed.
    assert "'logo.avif'" in js
    # 'hero.png' is also unique → rewritten too.
    assert "'hero.avif'" in js

    # Duplicate-basename warning for banner.png (small twin was not converted).
    assert any("banner.png" in w and "duplicate basename" in w for w in r.warnings)

    # Page got smaller, math is consistent.
    assert r.output_bytes < r.source_bytes
    assert r.ratio > 1.0
    assert r.arweave_savings_usd == pytest.approx(
        r.arweave_cost_usd_before - r.arweave_cost_usd_after, abs=1e-6
    )

    # Manifest: valid JSON, hashes match the bytes on disk.
    m = json.loads(r.manifest_path.read_text(encoding="utf-8"))
    assert m["totals"]["output_bytes"] == r.output_bytes
    hero_entry = next(f for f in m["files"] if f["path"] == "img/hero.avif")
    on_disk = hashlib.sha256((out / "img" / "hero.avif").read_bytes()).hexdigest()
    assert hero_entry["proof_hash"] == on_disk
    assert hero_entry["action"] == "converted:avif"


@needs_avif
def test_existing_output_needs_force(tmp_path):
    site = make_site(tmp_path / "site")
    compress_page(site)
    with pytest.raises(FileExistsError):
        compress_page(site)
    r = compress_page(site, force=True)
    assert r.output_dir.exists()


def test_nested_output_rejected(tmp_path):
    site = make_site(tmp_path / "site")
    with pytest.raises(ValueError, match="nested"):
        compress_page(site, output_dir=site / "out")
    with pytest.raises(ValueError, match="differ"):
        compress_page(site, output_dir=site)


def test_webp_page(tmp_path):
    site = make_site(tmp_path / "site")
    r = compress_page(site, fmt="webp")
    assert (r.output_dir / "img" / "hero.webp").exists()
    index = (r.output_dir / "index.html").read_text(encoding="utf-8")
    assert "img/hero.webp" in index


@needs_avif
def test_no_manifest_flag(tmp_path):
    site = make_site(tmp_path / "site")
    r = compress_page(site, write_manifest=False)
    assert r.manifest_path is None
    assert not (r.output_dir / "nebula_page_manifest.json").exists()
