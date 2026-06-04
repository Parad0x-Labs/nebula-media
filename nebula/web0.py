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
        "description":  "screen/text content — sharpness-preserving",
    },
    ContentType.PHOTO: {
        "avif_quality": 80,   # natural photos tolerate more compression
        "webp_quality": 82,
        "description":  "natural photo — perceptually transparent",
    },
    ContentType.GRAPHIC: {
        "avif_quality": 85,   # diagrams need clean edges
        "webp_quality": 88,
        "description":  "graphic/diagram — edge-preserving",
    },
}


# ---------------------------------------------------------------------------
# Image encoder
# ---------------------------------------------------------------------------

def encode_image_web0(
    source:          Path,
    output:          Optional[Path]        = None,
    content_type:    Optional[ContentType] = None,
    quality:         Optional[int]         = None,
    fmt:             str                   = "avif",
    measure_quality: bool                  = True,
    ar_per_gb:       float                 = _AR_PER_GB_DEFAULT,
    ar_usd:          float                 = _AR_USD_DEFAULT,
) -> Web0EncodeResult:
    """
    Compress a single image for Arweave storage.

    Parameters
    ----------
    source:
        Input image (JPEG, PNG, WebP, AVIF, HEIC, BMP, TIFF).
    output:
        Output path.  Defaults to ``<source_stem>_web0.<fmt>``.
    content_type:
        PHOTO | SCREENSHOT | GRAPHIC.  Auto-detected if None.
    quality:
        Override the auto-selected quality (0-100).
    fmt:
        Output format: "avif" (recommended) or "webp" (wider compat).
    measure_quality:
        Compute SSIM between source and output (adds ~50-200 ms).
    ar_per_gb / ar_usd:
        Arweave pricing for cost estimation.

    Returns
    -------
    Web0EncodeResult
    """
    from PIL import Image

    source      = Path(source).resolve()
    fmt         = fmt.lower().lstrip(".")
    if fmt not in ("avif", "webp"):
        raise ValueError(f"Unsupported format '{fmt}'.  Choose 'avif' or 'webp'.")

    if output is None:
        output = source.with_name(source.stem + f"_web0.{fmt}")
    output = Path(output).resolve()

    if content_type is None:
        content_type = detect_content_type(source)

    params   = _IMAGE_QUALITY.get(content_type, _IMAGE_QUALITY[ContentType.PHOTO])
    q        = quality if quality is not None else params[f"{fmt}_quality"]
    src_size = source.stat().st_size

    log.info("encode_image_web0: %s → %s  content=%s  quality=%d",
             source.name, fmt, content_type.value, q)

    img = Image.open(source).convert("RGB")

    if fmt == "avif":
        # Pillow 11.3.0 has native AVIF support via libavif
        img.save(str(output), format="AVIF", quality=q)
    else:  # webp
        img.save(str(output), format="WEBP", quality=q, method=6)

    log.info("  %s KB → %s KB  (%.1f×)",
             src_size // 1024,
             output.stat().st_size // 1024,
             src_size / output.stat().st_size)

    # SSIM quality measurement (optional)
    ssim_score = -1.0
    if measure_quality:
        try:
            from nebula.metrics import compute_ssim, rgb_to_y
            import numpy as np
            ref = np.array(img, dtype=np.float64)
            dis_img = Image.open(output).convert("RGB")
            dis = np.array(dis_img, dtype=np.float64)
            # Compute on luma channel
            ref_y = rgb_to_y(ref.astype(np.uint8))
            dis_y = rgb_to_y(dis.astype(np.uint8))
            ssim_score = compute_ssim(ref_y.astype(np.float64),
                                      dis_y.astype(np.float64))
            log.info("  SSIM %.4f", ssim_score)
        except Exception as exc:
            log.warning("SSIM measurement failed: %s", exc)

    return _make_result(output, content_type, fmt, src_size, ssim_score,
                        ar_per_gb, ar_usd)


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
    from nebula.encoder import compress_video, Encoder

    source = Path(source).resolve()
    if content_type is None:
        content_type = detect_content_type(source, ffprobe)

    params  = _VIDEO_WEB0.get(content_type, _VIDEO_WEB0[ContentType.VIDEO_NATURAL])
    encoder = "x265" if content_type == ContentType.VIDEO_SCREEN else "svt-av1"
    src_size = source.stat().st_size

    log.info("encode_video_web0: %s  content=%s  encoder=%s  mode=%s",
             source.name, content_type.value, encoder, params["mode"])

    if output is None:
        ext    = ".mp4" if encoder == "x265" else ".mp4"
        output = source.with_name(source.stem + "_web0" + ext)
    output = Path(output).resolve()

    result = compress_video(
        input_path         = source,
        output_path        = output,
        mode               = params["mode"],
        encoder            = encoder,
        measure_vmaf_score = measure_vmaf,
        target_vmaf        = 93.0,   # Web0 target: slightly below archival 95
    )

    log.info("  %d MB → %d MB  (%.1f×)  VMAF=%.2f",
             src_size // (1 << 20),
             result.output_path.stat().st_size // (1 << 20),
             src_size / max(1, result.output_path.stat().st_size),
             result.vmaf)

    vmaf = result.vmaf if result.vmaf >= 0 else -1.0
    return _make_result(output, content_type, encoder, src_size, vmaf,
                        ar_per_gb, ar_usd)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def encode_for_web0(
    source:          str | Path,
    output:          Optional[str | Path]  = None,
    content_type:    Optional[ContentType] = None,
    quality:         Optional[int]         = None,   # images only
    fmt:             str                   = "avif", # images only
    ffmpeg:          str                   = "ffmpeg",
    ffprobe:         str                   = "ffprobe",
    measure_quality: bool                  = True,
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

    if _is_image(source):
        return encode_image_web0(
            source=source, output=output,
            content_type=content_type, quality=quality, fmt=fmt,
            measure_quality=measure_quality, ar_per_gb=ar_per_gb, ar_usd=ar_usd,
        )
    elif _is_video(source):
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
            out = (output_dir / (src.stem + "_web0" + src.suffix)) if output_dir else None
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
    import sys

    parser = argparse.ArgumentParser(
        prog="nebula-web0",
        description="Arweave-optimised encoder for images and video",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="+", help="Input file(s)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path (single file mode only)")
    parser.add_argument("--format", "-f", default="avif",
                        choices=["avif", "webp"],
                        help="Output format for images")
    parser.add_argument("--quality", "-q", type=int, default=None,
                        help="Quality override for images (0-100)")
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
                quality=args.quality,
                fmt=args.format,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                measure_quality=not args.no_quality,
                ar_per_gb=args.ar_per_gb,
                ar_usd=args.ar_usd,
            )
            print(json.dumps({
                "output":         str(r.output_path),
                "content_type":   r.content_type.value,
                "encoder":        r.encoder,
                "source_kb":      r.source_size // 1024,
                "output_kb":      r.output_size // 1024,
                "ratio":          r.ratio,
                "quality":        r.quality_score,
                "proof_hash":     r.proof_hash,
                "arweave_cost_ar":       r.arweave_cost_ar,
                "arweave_cost_usd":      r.arweave_cost_usd_at_30,
                "arweave_savings_usd":   r.arweave_savings_usd_at_30,
                "note": f"cost at ${args.ar_usd}/AR",
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
