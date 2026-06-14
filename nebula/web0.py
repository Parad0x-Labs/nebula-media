# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Parad0x Labs
"""
nebula/web0.py — Web0 / Arweave-optimised encoding for video and images.

Arweave storage is permanent and pay-per-byte.  Every byte you upload costs
AR tokens and lives on-chain for 200 years.  This module encodes media at the
optimal quality/size trade-off for that constraint:

  * Perceptually excellent — not archival, not low-quality streaming.
  * Smallest file that a human cannot distinguish from the source.
  * AVIF for images (50-90 % smaller than JPEG/PNG at equal quality).
  * AV1 for video (VMAF 93-95 target, smallest at that quality floor).
  * Screen content auto-detected and routed to sharpness-preserving settings.
  * Cost estimate in AR and USD included in every result.

Public API
----------
    from nebula.web0 import encode_for_web0, estimate_arweave_cost

    # Auto-detects image vs video, content type, picks encoder
    result = encode_for_web0("screenshot.png")
    result = encode_for_web0("recording.mp4")

    print(result.ratio, result.arweave_cost_usd_at_30)

CLI
---
    python -m nebula.web0 input.png
    python -m nebula.web0 input.mp4 --quality 90
    python -m nebula.web0 input.jpg --format webp
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("nebula.web0")

# ---------------------------------------------------------------------------
# Arweave cost model
# ---------------------------------------------------------------------------

# Cost per GB of Arweave storage (in AR tokens).
# Arweave uses a price oracle; this figure is an approximation as of mid-2026.
# Pass ar_per_gb and ar_usd to estimate_arweave_cost() to use live prices.
_AR_PER_GB_DEFAULT  = 0.628   # AR tokens per GB
_AR_USD_DEFAULT     = 30.0    # USD per AR token


def estimate_arweave_cost(
    size_bytes: int,
    ar_per_gb: float = _AR_PER_GB_DEFAULT,
    ar_usd: float    = _AR_USD_DEFAULT,
) -> dict:
    """
    Estimate the one-time Arweave storage cost for a file of *size_bytes*.

    Returns a dict with 'ar', 'usd', 'mb', and 'usd_per_mb_rate'.
    Arweave storage is permanent — the cost is paid once at upload time.
    """
    mb  = size_bytes / 1_048_576
    gb  = size_bytes / 1_073_741_824
    ar  = gb * ar_per_gb
    usd = ar * ar_usd
    return {
        "ar":            round(ar,  8),
        "usd":           round(usd, 6),
        "mb":            round(mb,  3),
        "usd_per_mb":    round(ar_usd * ar_per_gb / 1024, 8),
    }


# ---------------------------------------------------------------------------
# Content type detection
# ---------------------------------------------------------------------------

class ContentType(str, Enum):
    PHOTO        = "photo"         # camera photos, natural images
    SCREENSHOT   = "screenshot"    # screen captures, UI, text
    GRAPHIC      = "graphic"       # diagrams, charts, simple illustrations
    VIDEO_NATURAL = "video_natural" # camera footage, films, nature
    VIDEO_SCREEN  = "video_screen"  # screen recordings, demos, tutorials

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif",
                     ".webp", ".avif", ".heic", ".heif", ".gif"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
                     ".ts", ".mts", ".m2ts"}


class PlatformTarget(str, Enum):
    """Where the output is going — determines codec, container, and limits."""
    UNIVERSAL = "universal"  # best quality+size: AV1/x265 video, AVIF image.
                             # For Arweave / storage / web0. Default.
    X         = "x"          # Twitter/X-compatible: H.264 ≤1080p + AAC + faststart
                             # (video), WebP (image). X rejects AV1/AVIF and caps
                             # at 1080p, so this is the upload-safe downgrade.

# X (Twitter) upload constraints (verified 2026):
#   - Codec: H.264 only (AV1/HEVC rejected for most accounts)
#   - Max resolution: 1920×1200 (1080p) — X re-encodes anything larger
#   - Audio: an AAC track is expected; silent video is often rejected
#   - X re-encodes uploads to ~2 Mbps 1080p regardless, so don't over-spend bits
#   - Duration: free accounts cap at 2:20 (140s); Premium allows hours
#   - File size: 512 MB free, GBs on Premium
_X_MAX_WIDTH      = 1920
_X_FREE_DURATION  = 140.0    # seconds — free-tier cap (Premium goes longer)
_X_VIDEO_CRF      = 20       # x264 CRF; clean 1080p source for X's own re-encode


def _require_ffmpeg(ffmpeg: str, ffprobe: str) -> None:
    """
    Raise a clear, actionable error if ffmpeg/ffprobe aren't available.

    Video encoding needs them; image and page modes do not.  Without this the
    failure surfaces as a cryptic "[Errno 2] No such file or directory:
    'ffprobe'", which a first-time user can't act on.
    """
    missing = [b for b in (ffmpeg, ffprobe)
               if shutil.which(b) is None and not Path(b).is_file()]
    if missing:
        raise RuntimeError(
            f"Video encoding needs ffmpeg + ffprobe on your PATH (not found: "
            f"{', '.join(missing)}).\n"
            f"  macOS:         brew install ffmpeg\n"
            f"  Ubuntu/Debian: sudo apt install ffmpeg\n"
            f"  Windows:       https://ffmpeg.org/download.html\n"
            f"(Image and .null page modes don't need ffmpeg — only video does.)"
        )


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_EXTENSIONS


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_EXTENSIONS


def detect_content_type(path: Path, ffprobe: str = "ffprobe") -> ContentType:
    """
    Auto-detect content type from file extension + codec metadata.

    For images:
      - PNG/screenshot heuristic: if width >= 1280 and the filename contains
        'screen', 'capture', 'screenshot', or 'recording' → SCREENSHOT.
      - Otherwise: PHOTO (safe default, AVIF quality 82).

    For video:
      - Uses VideoInfo.is_screen_content from nebula.encoder.probe_video.
    """
    if _is_image(path):
        name_lower = path.stem.lower()
        screen_keywords = {"screen", "screenshot", "capture", "recording",
                           "snapshot", "clip", "window"}
        if any(k in name_lower for k in screen_keywords):
            return ContentType.SCREENSHOT
        # PNG is usually a screenshot
        if path.suffix.lower() == ".png":
            return ContentType.SCREENSHOT
        return ContentType.PHOTO

    if _is_video(path):
        try:
            from nebula.encoder import probe_video
            info = probe_video(path, ffprobe)
            return ContentType.VIDEO_SCREEN if info.is_screen_content else ContentType.VIDEO_NATURAL
        except Exception:
            return ContentType.VIDEO_NATURAL

    return ContentType.PHOTO


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class Web0EncodeResult:
    """Returned by encode_for_web0 / encode_image_web0 / encode_video_web0."""

    output_path:    Path
    content_type:   ContentType
    encoder:        str          # e.g. "avif", "webp", "svt-av1"
    source_size:    int          # bytes
    output_size:    int          # bytes
    ratio:          float        # source / output — higher = more compression
    quality_score:  float        # SSIM (image) or VMAF (video); -1.0 if skipped
    proof_hash:     str          # SHA-256 of output file

    # Arweave cost (at $30/AR by default)
    arweave_cost_ar:       float
    arweave_cost_usd_at_30: float
    # How much was saved vs storing the original
    arweave_savings_usd_at_30: float

    ar_per_gb:  float = _AR_PER_GB_DEFAULT
    ar_usd:     float = _AR_USD_DEFAULT

    quality_setting: int = -1
    """Final encoder quality setting used (0-100 for images; -1 when not applicable)."""

    note: str = ""
    """Human-readable note, e.g. why the original file was kept instead of re-encoded."""

    def cost_at(self, ar_usd: float) -> float:
        """Cost in USD at a custom AR/USD exchange rate."""
        return self.arweave_cost_ar * ar_usd

    def summary(self) -> str:
        return (
            f"{self.content_type.value}  {self.encoder}  "
            f"{self.source_size//1024}KB→{self.output_size//1024}KB "
            f"({self.ratio:.1f}×)  "
            f"quality={self.quality_score:.2f}  "
            f"cost=${self.arweave_cost_usd_at_30:.4f}  "
            f"saves=${self.arweave_savings_usd_at_30:.4f}"
        )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_result(
    output_path: Path,
    content_type: ContentType,
    encoder: str,
    source_size: int,
    quality_score: float,
    ar_per_gb: float = _AR_PER_GB_DEFAULT,
    ar_usd: float    = _AR_USD_DEFAULT,
    quality_setting: int = -1,
    note: str = "",
) -> Web0EncodeResult:
    output_size = output_path.stat().st_size
    ratio       = source_size / output_size if output_size else 0.0
    cost_out    = estimate_arweave_cost(output_size,  ar_per_gb, ar_usd)
    cost_src    = estimate_arweave_cost(source_size,  ar_per_gb, ar_usd)
    savings     = cost_src["usd"] - cost_out["usd"]
    return Web0EncodeResult(
        output_path               = output_path,
        content_type              = content_type,
        encoder                   = encoder,
        source_size               = source_size,
        output_size               = output_size,
        ratio                     = round(ratio, 2),
        quality_score             = round(quality_score, 4),
        proof_hash                = _sha256(output_path),
        arweave_cost_ar           = cost_out["ar"],
        arweave_cost_usd_at_30    = cost_out["usd"],
        arweave_savings_usd_at_30 = round(savings, 6),
        ar_per_gb                 = ar_per_gb,
        ar_usd                    = ar_usd,
        quality_setting           = quality_setting,
        note                      = note,
    )


# ---------------------------------------------------------------------------
# Image quality settings for Arweave
# ---------------------------------------------------------------------------

# Quality targets calibrated for Arweave permanent storage:
#   * High enough to be visually indistinguishable on a display
#   * Low enough to save meaningful storage cost
#   * Text/screenshot content uses higher quality to keep glyphs crisp
#
# AVIF quality scale: 0-100, higher = better quality / larger file
_IMAGE_QUALITY: dict[ContentType, dict] = {
    ContentType.SCREENSHOT: {
        "avif_quality": 88,   # text must stay crisp — higher quality
        "webp_quality": 92,
        "ssim_floor":   0.96, # below this the encode is retried at higher quality
        "description":  "screen/text content — sharpness-preserving",
    },
    ContentType.PHOTO: {
        "avif_quality": 80,   # natural photos tolerate more compression
        "webp_quality": 82,
        "ssim_floor":   0.92,
        "description":  "natural photo — perceptually transparent",
    },
    ContentType.GRAPHIC: {
        "avif_quality": 85,   # diagrams need clean edges
        "webp_quality": 88,
        "ssim_floor":   0.94,
        "description":  "graphic/diagram — edge-preserving",
    },
}


# ---------------------------------------------------------------------------
# Image encoder
# ---------------------------------------------------------------------------

def _require_avif_support() -> None:
    """Raise a clear error when Pillow was installed without AVIF support."""
    from PIL import features
    try:
        ok = features.check("avif")
    except Exception:
        ok = False
    if not ok:
        raise RuntimeError(
            "This Pillow build has no AVIF support (needs pillow>=11.3).  "
            "Fix with:  pip install --upgrade 'pillow>=11.3'  — or pass fmt='webp'."
        )


def _composite_white(im) -> "object":
    """Return an RGB copy of *im*, alpha-composited over white when transparent."""
    from PIL import Image
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg   = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return im.convert("RGB")


def _image_ssim(ref_img, encoded_path: Path) -> float:
    """
    SSIM (luma, BT.709) between an in-memory reference image and an encoded
    file on disk.  Transparent images are composited over white on BOTH sides
    so alpha is compared fairly.  Returns -1.0 if measurement fails.
    """
    try:
        import math
        import numpy as np
        from PIL import Image
        from nebula.metrics import compute_ssim, rgb_to_y
        # compute_ssim uses an 11x11 gaussian window and crops a 5 px border —
        # anything smaller than 22 px per side has no valid SSIM region.
        if ref_img.size[0] < 22 or ref_img.size[1] < 22:
            log.debug("image %s too small for SSIM — skipping", ref_img.size)
            return -1.0
        dis_img = Image.open(encoded_path)
        dis_img.load()
        ref_y = rgb_to_y(np.asarray(_composite_white(ref_img), dtype=np.uint8))
        dis_y = rgb_to_y(np.asarray(_composite_white(dis_img), dtype=np.uint8))
        score = compute_ssim(ref_y.astype(np.float64), dis_y.astype(np.float64))
        return -1.0 if math.isnan(score) else score
    except Exception as exc:
        log.warning("SSIM measurement failed: %s", exc)
        return -1.0


def _save_image(img, output: Path, fmt: str, q: int) -> None:
    """Encode *img* to *output* as AVIF or WebP at quality *q*, keeping ICC."""
    kwargs: dict = {"quality": q}
    icc = img.info.get("icc_profile")
    if icc:
        kwargs["icc_profile"] = icc
    if fmt == "avif":
        img.save(str(output), format="AVIF", **kwargs)
    else:  # webp
        img.save(str(output), format="WEBP", method=6, **kwargs)


def encode_image_web0(
    source:          Path,
    output:          Optional[Path]        = None,
    content_type:    Optional[ContentType] = None,
    quality:         Optional[int]         = None,
    fmt:             str                   = "avif",
    measure_quality: bool                  = True,
    target_ssim:     Optional[float]       = None,
    max_quality_retries: int               = 2,
    keep_original_if_larger: bool          = True,
    ar_per_gb:       float                 = _AR_PER_GB_DEFAULT,
    ar_usd:          float                 = _AR_USD_DEFAULT,
) -> Web0EncodeResult:
    """
    Compress a single image for Arweave storage.

    Correctness guarantees (added for Web0/.null page publishing):

    * **Alpha is preserved** — transparent PNG/WebP sources stay transparent
      in the AVIF/WebP output (no silent flatten-to-black).
    * **EXIF orientation is applied** — rotated camera JPEGs stay upright
      even though the EXIF block itself is not carried into the output.
    * **Never grows a file** — if the encode is >= the source size, the
      original is kept (``encoder == "copy"``) so a page never gets bigger.
    * **SSIM floor with retry** — if the measured SSIM lands below the
      content-type floor (or *target_ssim*), the encode retries at higher
      quality (up to *max_quality_retries* times).
    * **Animated GIFs are refused** — the still path would silently keep
      only frame 1; convert animations with the video path instead.

    Parameters
    ----------
    source:
        Input image (JPEG, PNG, WebP, AVIF, BMP, TIFF; HEIC if your Pillow
        build decodes it).
    output:
        Output path.  Defaults to ``<source_stem>_web0.<fmt>``.
    content_type:
        PHOTO | SCREENSHOT | GRAPHIC.  Auto-detected if None.
    quality:
        Override the auto-selected quality (0-100).  An explicit quality
        disables the SSIM retry unless *target_ssim* is also given.
    fmt:
        Output format: "avif" (recommended) or "webp" (wider compat).
    measure_quality:
        Compute SSIM between source and output (adds ~50-200 ms).  Required
        for the SSIM-floor retry to function.
    target_ssim:
        Explicit SSIM floor (overrides the content-type default).
    max_quality_retries:
        Maximum quality bumps (+8 each) when SSIM is below the floor.
    keep_original_if_larger:
        Keep a copy of the source instead of a bigger "compressed" file.
    ar_per_gb / ar_usd:
        Arweave pricing for cost estimation.

    Returns
    -------
    Web0EncodeResult
    """
    from PIL import Image, ImageOps

    source      = Path(source).resolve()
    fmt         = fmt.lower().lstrip(".")
    if fmt not in ("avif", "webp"):
        raise ValueError(f"Unsupported format '{fmt}'.  Choose 'avif' or 'webp'.")
    if fmt == "avif":
        _require_avif_support()

    if output is None:
        output = source.with_name(source.stem + f"_web0.{fmt}")
    output = Path(output).resolve()

    if content_type is None:
        content_type = detect_content_type(source)

    params   = _IMAGE_QUALITY.get(content_type, _IMAGE_QUALITY[ContentType.PHOTO])
    q        = quality if quality is not None else params[f"{fmt}_quality"]
    floor    = target_ssim if target_ssim is not None else params["ssim_floor"]
    src_size = source.stat().st_size

    log.info("encode_image_web0: %s → %s  content=%s  quality=%d",
             source.name, fmt, content_type.value, q)

    img = Image.open(source)
    if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
        raise ValueError(
            f"'{source.name}' is animated ({img.n_frames} frames) — the still-image "
            "path would keep only the first frame.  Encode it as video instead "
            "(encode_video_web0), or pass a single-frame export."
        )
    img = ImageOps.exif_transpose(img)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    img = img.convert("RGBA" if has_alpha else "RGB")

    # An explicit quality with no explicit floor means "trust my setting".
    retry_enabled = measure_quality and not (quality is not None and target_ssim is None)

    attempts   = 0
    ssim_score = -1.0
    while True:
        _save_image(img, output, fmt, q)
        if measure_quality:
            ssim_score = _image_ssim(img, output)
        if (retry_enabled
                and 0.0 <= ssim_score < floor
                and attempts < max_quality_retries
                and q < 95):
            attempts += 1
            q = min(95, q + 8)
            log.info("  SSIM %.4f below floor %.3f — retry %d/%d at quality %d",
                     ssim_score, floor, attempts, max_quality_retries, q)
            continue
        break

    if retry_enabled and 0.0 <= ssim_score < floor:
        log.warning("  SSIM %.4f still below floor %.3f after %d retries — "
                    "keeping best attempt (quality %d)",
                    ssim_score, floor, attempts, q)

    out_size      = output.stat().st_size
    encoder_label = fmt
    note          = ""

    if keep_original_if_larger and out_size >= src_size:
        # The "compressed" file is not smaller — keep the original bytes so a
        # page never grows.  The kept file gets the source's own extension so
        # bytes always match the file name.
        output.unlink(missing_ok=True)
        kept = output.with_suffix(source.suffix)
        if kept != source:
            shutil.copy2(source, kept)
        output        = kept
        encoder_label = "copy"
        ssim_score    = 1.0
        note          = (f"{fmt} at quality {q} was {out_size} B >= source "
                         f"{src_size} B — original kept")
        log.info("  %s", note)
    else:
        log.info("  %s KB → %s KB  (%.1f×)%s",
                 src_size // 1024, out_size // 1024,
                 src_size / out_size if out_size else 0.0,
                 f"  SSIM {ssim_score:.4f}" if ssim_score >= 0 else "")

    return _make_result(output, content_type, encoder_label, src_size, ssim_score,
                        ar_per_gb, ar_usd, quality_setting=q, note=note)


# ---------------------------------------------------------------------------
# Video encoder
# ---------------------------------------------------------------------------

# Video quality settings for Arweave permanent storage.
# Slightly less aggressive than archival but significantly smaller.
_VIDEO_WEB0: dict[ContentType, dict] = {
    ContentType.VIDEO_SCREEN: {
        "encoder":   "x265",   # screen codec AV1 path available via --encoder svt-av1
        "crf_x265":  23,       # same as balanced — sharp text
        "crf_av1":   30,       # tighter than balanced (32) for small files
        "mode":      "balanced",
        "description": "screen recording — text-sharp, AV1 preferred",
    },
    ContentType.VIDEO_NATURAL: {
        "encoder":   "svt-av1",
        "crf_x265":  24,
        "crf_av1":   32,       # VMAF ~95, ratio ~3.5× vs source
        "mode":      "balanced",
        "description": "natural video — AV1 balanced, VMAF 95 target",
    },
}


def encode_video_web0(
    source:          Path,
    output:          Optional[Path]        = None,
    content_type:    Optional[ContentType] = None,
    ffmpeg:          str                   = "ffmpeg",
    ffprobe:         str                   = "ffprobe",
    measure_vmaf:    bool                  = True,
    ar_per_gb:       float                 = _AR_PER_GB_DEFAULT,
    ar_usd:          float                 = _AR_USD_DEFAULT,
) -> Web0EncodeResult:
    """
    Compress a video for Arweave storage using nebula's encoder pipeline.

    Automatically routes:
      - Screen recordings → x265 balanced with screen preset
      - Natural video → SVT-AV1 balanced (VMAF ~95, fastest at that quality)

    Calls nebula.encoder.compress_video() with Web0-optimised settings.
    """
    from nebula.encoder import compress_video

    _require_ffmpeg(ffmpeg, ffprobe)
    source = Path(source).resolve()
    if content_type is None:
        content_type = detect_content_type(source, ffprobe)

    params   = _VIDEO_WEB0.get(content_type, _VIDEO_WEB0[ContentType.VIDEO_NATURAL])
    src_size = source.stat().st_size

    # Do NOT hardcode the encoder for VIDEO_NATURAL.  Grain detection in
    # select_encoder() must run after probe_video() — only it knows the actual
    # grain_level.  Forcing "svt-av1" here bypasses that check and sends
    # grainy film through AV1 film-grain synthesis, which collapses quality
    # (measured: Jellyfish grain=1.0 → AV1 VMAF 54, x265 VMAF 95.44).
    # Screen content is safe to force x265 because is_screen_content is set
    # before probe, but natural video must let select_encoder() decide.
    forced_encoder = "x265" if content_type == ContentType.VIDEO_SCREEN else None

    log.info("encode_video_web0: %s  content=%s  encoder=%s  mode=%s",
             source.name, content_type.value,
             forced_encoder or "auto (grain-aware)", params["mode"])

    if output is None:
        output = source.with_name(source.stem + "_web0.mp4")
    output = Path(output).resolve()

    result = compress_video(
        input_path         = source,
        output_path        = output,
        mode               = params["mode"],
        encoder            = forced_encoder,   # None = auto-route by grain_level
        measure_vmaf_score = measure_vmaf,
        target_vmaf        = 93.0,   # Web0 target: slightly below archival 95
    )

    log.info("  %d MB → %d MB  (%.1f×)  VMAF=%.2f",
             src_size // (1 << 20),
             result.output_path.stat().st_size // (1 << 20),
             src_size / max(1, result.output_path.stat().st_size),
             result.vmaf)

    vmaf = result.vmaf if result.vmaf >= 0 else -1.0
    # Use the actual encoder selected (result.encoder reflects auto-routing)
    return _make_result(output, content_type, result.encoder, src_size, vmaf,
                        ar_per_gb, ar_usd)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def encode_for_x(
    source:          Path,
    output:          Optional[Path] = None,
    ffmpeg:          str            = "ffmpeg",
    ffprobe:         str            = "ffprobe",
    crf:             int            = _X_VIDEO_CRF,
    max_width:       int            = _X_MAX_WIDTH,
    measure_quality: bool           = True,
    ar_per_gb:       float          = _AR_PER_GB_DEFAULT,
    ar_usd:          float          = _AR_USD_DEFAULT,
) -> Web0EncodeResult:
    """
    Encode a video for direct Twitter/X upload (the upload-safe downgrade).

    X rejects AV1, caps at 1080p, expects an AAC audio track, and re-encodes
    every upload to ~2 Mbps regardless.  So this produces:
      * H.264 High profile, level 4.2, yuv420p
      * downscaled so the long edge <= 1920 (never upscaled)
      * AAC audio — a silent stereo track is synthesised if the source has none
      * +faststart (moov atom at front)

    Warns if duration exceeds the free-tier 2:20 cap (needs X Premium).
    This is intentionally NOT the quality/size-optimal encode — for that, use
    the universal target (AV1/x265).  This trades efficiency for X compatibility.
    """
    from nebula.encoder import probe_video

    _require_ffmpeg(ffmpeg, ffprobe)
    source = Path(source).resolve()
    info   = probe_video(source, ffprobe)
    if output is None:
        output = source.with_name(source.stem + "_X.mp4")
    output = Path(output).resolve()

    if info.duration > _X_FREE_DURATION:
        log.warning(
            "duration %.0fs exceeds X free-tier cap (%.0fs / 2:20) — "
            "needs X Premium to post the full length.",
            info.duration, _X_FREE_DURATION,
        )

    src_size  = source.stat().st_size
    has_audio = info.has_audio
    # Cap the long edge to max_width; never upscale (min picks iw when iw<max).
    vf = (f"scale='min({max_width},iw)':-2"
          if info.width >= info.height
          else f"scale=-2:'min({max_width},ih)'")

    log.info("encode_for_x: %s  %dx%d→≤%dp  h264 crf%d  audio=%s",
             source.name, info.width, info.height, max_width, crf,
             "copy" if has_audio else "synth-silent")

    cmd: list = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += [
        "-vf", vf,
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.2",
        "-pix_fmt", "yuv420p", "-crf", str(crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", "128k",
    ]
    # Map ONLY the first video + first audio stream, and kill data/timecode
    # tracks. .mov sources (screen recordings, camera footage) carry a tmcd
    # timecode track that X can choke on. Explicit mapping isn't enough — the
    # mp4 muxer re-creates tmcd from the source's `timecode` metadata, so we
    # also need -dn (no data streams), -write_tmcd 0 (no muxer-generated tmcd),
    # and -map_metadata -1 (drop the timecode tag that triggers it).
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    # NOTE: do NOT use -map_metadata -1 here — it would drop the rotation flag
    # and flip portrait phone video sideways. -dn + -write_tmcd 0 kill the tmcd
    # timecode track while leaving rotation/orientation metadata intact.
    cmd += ["-dn", "-write_tmcd", "0",
            "-movflags", "+faststart", str(output)]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"encode_for_x failed (exit {proc.returncode}):\n"
                           f"{proc.stderr[-1500:]}")

    vmaf = -1.0
    if measure_quality:
        try:
            import json as _json
            out_info = probe_video(output, ffprobe)
            vlog = output.with_suffix(".x.vmaf.json")
            mcmd = [
                ffmpeg, "-nostdin", "-loglevel", "error",
                "-i", str(output), "-i", str(source),
                "-lavfi",
                (f"[1:v]scale={out_info.width}:{out_info.height}:flags=lanczos[ref];"
                 f"[0:v][ref]libvmaf=model=version=vmaf_v0.6.1:"
                 f"n_threads=8:n_subsample=10:log_fmt=json:log_path={vlog}"),
                "-f", "null", "-",
            ]
            subprocess.run(mcmd, capture_output=True, timeout=1800)
            with open(vlog) as fh:
                vmaf = round(float(_json.load(fh)["pooled_metrics"]["vmaf"]["mean"]), 2)
            log.info("  X-file VMAF %.2f (vs source @ output resolution)", vmaf)
        except Exception as exc:
            log.warning("X VMAF measurement failed: %s", exc)

    return _make_result(output, ContentType.VIDEO_NATURAL, "h264", src_size, vmaf,
                        ar_per_gb, ar_usd,
                        note="X-compatible: H.264 1080p (downgrade from universal AV1)")


def encode_for_web0(
    source:          str | Path,
    output:          Optional[str | Path]  = None,
    target:          str                   = "universal",  # "universal" | "x"
    content_type:    Optional[ContentType] = None,
    quality:         Optional[int]         = None,   # images only
    fmt:             str                   = "avif", # images only
    ffmpeg:          str                   = "ffmpeg",
    ffprobe:         str                   = "ffprobe",
    measure_quality: bool                  = True,
    target_ssim:     Optional[float]       = None,   # images only
    ar_per_gb:       float                 = _AR_PER_GB_DEFAULT,
    ar_usd:          float                 = _AR_USD_DEFAULT,
) -> Web0EncodeResult:
    """
    Unified Arweave-optimised encoder.  Auto-detects image vs video.

    Parameters
    ----------
    source:
        Any image or video file.
    output:
        Output path.  Defaults to ``<source_stem>_web0.<ext>``.
    quality:
        Image quality override (0-100).  Ignored for video.
    fmt:
        Image output format: "avif" or "webp".  Ignored for video.
    ar_per_gb / ar_usd:
        Arweave pricing parameters for cost estimation.

    Examples
    --------
    >>> r = encode_for_web0("screenshot.png")
    >>> print(r.summary())
    screenshot avif 2048KB→180KB (11.4×) quality=0.9982 cost=$0.0000 saves=$0.0001

    >>> r = encode_for_web0("recording.mp4")
    >>> print(f"{r.ratio:.1f}× smaller, saves ${r.arweave_savings_usd_at_30:.4f} on Arweave")
    """
    source = Path(source).resolve()
    if output is not None:
        output = Path(output).resolve()

    target = target.lower()
    if target not in ("universal", "x"):
        raise ValueError(f"Unknown target '{target}'. Choose 'universal' or 'x'.")

    if _is_image(source):
        # X accepts WebP/JPEG/PNG but NOT AVIF — downgrade format for X target.
        img_fmt = "webp" if (target == "x" and fmt == "avif") else fmt
        return encode_image_web0(
            source=source, output=output,
            content_type=content_type, quality=quality, fmt=img_fmt,
            measure_quality=measure_quality, target_ssim=target_ssim,
            ar_per_gb=ar_per_gb, ar_usd=ar_usd,
        )
    elif _is_video(source):
        if target == "x":
            return encode_for_x(
                source=source, output=output,
                ffmpeg=ffmpeg, ffprobe=ffprobe,
                measure_quality=measure_quality,
                ar_per_gb=ar_per_gb, ar_usd=ar_usd,
            )
        return encode_video_web0(
            source=source, output=output,
            content_type=content_type,
            ffmpeg=ffmpeg, ffprobe=ffprobe,
            measure_vmaf=measure_quality,
            ar_per_gb=ar_per_gb, ar_usd=ar_usd,
        )
    else:
        raise ValueError(
            f"Cannot determine type for '{source.suffix}'.  "
            "Pass content_type explicitly or use encode_image_web0 / encode_video_web0 directly."
        )


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------

def batch_encode_web0(
    sources:         list[Path],
    output_dir:      Optional[Path]        = None,
    fmt:             str                   = "avif",
    ffmpeg:          str                   = "ffmpeg",
    ffprobe:         str                   = "ffprobe",
    ar_per_gb:       float                 = _AR_PER_GB_DEFAULT,
    ar_usd:          float                 = _AR_USD_DEFAULT,
) -> list[Web0EncodeResult]:
    """
    Encode a list of images and/or videos for Arweave storage.

    All results are returned in input order.  Failed encodes are skipped
    with a warning (not raised) so the batch continues.
    """
    results = []
    total_src  = 0
    total_out  = 0
    for src in sources:
        try:
            out = None
            if output_dir:
                # Name the output after the bytes it will actually contain —
                # the original suffix would label an AVIF file ".png".
                out_suffix = f".{fmt}" if _is_image(src) else ".mp4"
                out = output_dir / (src.stem + "_web0" + out_suffix)
            r = encode_for_web0(src, out, fmt=fmt, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                 ar_per_gb=ar_per_gb, ar_usd=ar_usd)
            results.append(r)
            total_src += r.source_size
            total_out += r.output_size
        except Exception as exc:
            log.warning("batch: failed %s — %s", src.name, exc)
    if results:
        batch_ratio   = total_src / total_out if total_out else 0
        batch_savings = estimate_arweave_cost(total_src, ar_per_gb, ar_usd)["usd"] - \
                        estimate_arweave_cost(total_out, ar_per_gb, ar_usd)["usd"]
        log.info(
            "batch done: %d files  %d KB→%d KB  (%.1f×)  saves $%.4f on Arweave",
            len(results), total_src // 1024, total_out // 1024,
            batch_ratio, batch_savings,
        )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="nebula-web0",
        description="Arweave-optimised encoder for images and video",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="+", help="Input file(s)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path (single file mode only)")
    parser.add_argument("--target", "-t", default="universal",
                        choices=["universal", "x"],
                        help="universal = best quality+size (AV1/AVIF, for Arweave); "
                             "x = Twitter/X-compatible (H.264 1080p video / WebP image)")
    parser.add_argument("--format", "-f", default="avif",
                        choices=["avif", "webp"],
                        help="Output format for images (universal target)")
    parser.add_argument("--content-type", default=None,
                        dest="content_type",
                        choices=[c.value for c in ContentType],
                        help="Override content-type auto-detection "
                             "(PNG defaults to 'screenshot' settings)")
    parser.add_argument("--quality", "-q", type=int, default=None,
                        help="Quality override for images (0-100)")
    parser.add_argument("--target-ssim", type=float, default=None,
                        dest="target_ssim", metavar="SSIM",
                        help="SSIM floor for images — retries at higher quality "
                             "if the encode lands below it (e.g. 0.96)")
    parser.add_argument("--ar-per-gb", type=float, default=_AR_PER_GB_DEFAULT,
                        metavar="AR",
                        help="Arweave storage cost in AR tokens per GB")
    parser.add_argument("--ar-usd", type=float, default=_AR_USD_DEFAULT,
                        metavar="USD",
                        help="Current AR/USD exchange rate")
    parser.add_argument("--no-quality", action="store_true",
                        help="Skip SSIM/VMAF measurement (faster)")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()

    inputs = [Path(p) for p in args.input]
    if len(inputs) == 1:
        try:
            r = encode_for_web0(
                source=inputs[0],
                output=Path(args.output) if args.output else None,
                target=args.target,
                content_type=ContentType(args.content_type) if args.content_type else None,
                quality=args.quality,
                fmt=args.format,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                measure_quality=not args.no_quality,
                target_ssim=args.target_ssim,
                ar_per_gb=args.ar_per_gb,
                ar_usd=args.ar_usd,
            )
            print(json.dumps({
                "output":         str(r.output_path),
                "content_type":   r.content_type.value,
                "encoder":        r.encoder,
                "quality_setting": r.quality_setting,
                "source_kb":      r.source_size // 1024,
                "output_kb":      r.output_size // 1024,
                "ratio":          r.ratio,
                "quality":        r.quality_score,
                "proof_hash":     r.proof_hash,
                "arweave_cost_ar":       r.arweave_cost_ar,
                "arweave_cost_usd":      r.arweave_cost_usd_at_30,
                "arweave_savings_usd":   r.arweave_savings_usd_at_30,
                "kept_original":  r.encoder == "copy",
                "note": (r.note + ("  " if r.note else "") + f"cost at ${args.ar_usd}/AR").strip(),
            }, indent=2))
        except Exception as exc:
            log.error("%s", exc)
            return 1
    else:
        results = batch_encode_web0(
            inputs, fmt=args.format,
            ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
            ar_per_gb=args.ar_per_gb, ar_usd=args.ar_usd,
        )
        total_savings = sum(r.arweave_savings_usd_at_30 for r in results)
        print(json.dumps({
            "files":            len(results),
            "total_source_kb":  sum(r.source_size for r in results) // 1024,
            "total_output_kb":  sum(r.output_size for r in results) // 1024,
            "avg_ratio":        round(sum(r.ratio for r in results) / len(results), 2) if results else 0,
            "total_arweave_savings_usd": round(total_savings, 4),
            "results":          [{"file": str(r.output_path), "ratio": r.ratio,
                                  "quality": r.quality_score,
                                  "cost_usd": r.arweave_cost_usd_at_30} for r in results],
        }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
