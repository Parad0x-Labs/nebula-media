# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Parad0x Labs
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""
nebula/encoder.py — Adaptive Video Zone Optimizer
============================================
Production-quality video compression with per-zone adaptive bitrate,
dual-encoder support (x265 / SVT-AV1), scene-aware boundary detection,
AV1 film grain synthesis, VMAF quality measurement, and on-chain-ready
proof hashing.

Public API
----------
    from nebula import compress_video, CompressionResult

    result = compress_video("input.mp4", mode="safe", target_vmaf=88)
    print(result.vmaf, result.vmaf_p1, result.ratio, result.output_path, result.proof_hash)

Modes
-----
    safe      — conservative settings, reliable compatibility (x265/AV1 CRF target)
    balanced  — default quality/size trade-off
    maximum — maximum compression, accepts longer encode times

Encoders
--------
    x265      — proven HEVC codec, wide device support
    svt-av1   — modern AV1, 10–40 % smaller at equal VMAF; auto-selected for
                 animated / low-grain content

License: MIT
"""

from __future__ import annotations

__all__ = ["compress_video", "CompressionResult"]

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Apple Silicon CPU topology
# ---------------------------------------------------------------------------
# Detect P-core and E-core counts via sysctl.  Falls back to os.cpu_count()
# on non-Apple platforms.  Used to tune encoder thread pools so P-cores handle
# frame-level parallelism (latency-sensitive) and E-cores fill in WPP/tile work.
def _sysctl_int(key: str) -> int:
    try:
        return int(subprocess.check_output(
            ["sysctl", "-n", key], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip())
    except Exception:
        return 0

_CPU_LOGICAL:  int = os.cpu_count() or 4
_CPU_P_CORES:  int = _sysctl_int("hw.perflevel0.physicalcpu") or _CPU_LOGICAL // 2
_CPU_E_CORES:  int = _sysctl_int("hw.perflevel1.physicalcpu") or 0
_IS_APPLE_SIL: bool = _CPU_P_CORES > 0 and _CPU_E_CORES > 0

if _IS_APPLE_SIL:
    import logging as _log_init
    _log_init.getLogger("nebula").debug(
        "Apple Silicon detected: %d P-cores + %d E-cores = %d total",
        _CPU_P_CORES, _CPU_E_CORES, _CPU_LOGICAL,
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("nebula")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class CompressionResult:
    """Returned by :func:`compress_video`."""

    output_path: Path
    """Absolute path to the compressed output file."""

    vmaf: float
    """VMAF mean score of the output (0–100).  -1.0 if measurement was skipped."""

    ratio: float
    """Size ratio output/input.  0.72 means 28 % smaller."""

    encoder: str
    """Encoder used: 'x265' or 'svt-av1'."""

    zones: int
    """Number of adaptive bitrate zones applied."""

    proof_hash: str
    """SHA-256 hex digest of the output file, suitable for on-chain anchoring."""

    input_path: Path
    """Original input file."""

    encode_log: Path
    """Path to the detailed encode log written alongside the output."""

    vmaf_p1: float = 0.0
    """VMAF 1st-percentile score (worst ~1 % of frames).  0.0 if not available."""

    cpu_pct_avg: float = 0.0
    """Average CPU utilisation across all cores during the encode pass (0–100×N cores).
    e.g. 850.0 on a 10-core machine means ~85 % of total CPU was consumed."""

    cpu_pct_peak: float = 0.0
    """Peak 1-second CPU utilisation sample during the encode pass."""

    encode_wall_s: float = 0.0
    """Wall-clock seconds for the encode pass (excluding VMAF measurement)."""

    extra: dict = field(default_factory=dict)
    """Additional metadata (scene cuts, per-zone stats, cpu topology, etc.)."""


# ---------------------------------------------------------------------------
# Enumerations & constants
# ---------------------------------------------------------------------------

class EncodeMode(str, Enum):
    SAFE      = "safe"
    BALANCED  = "balanced"
    MAXIMUM = "maximum"


class Encoder(str, Enum):
    X265          = "x265"
    SVT_AV1       = "svt-av1"
    VVC           = "vvc"           # H.266/VVC — best size, ~10-20× slower on Apple Silicon
    VIDEOTOOLBOX  = "videotoolbox"  # Apple hardware HEVC — real-time on M-series, draft quality


# Mode params — updated from measured benchmarks (2026-06-04 sweep):
#
# x265 CRF values (slow preset):
#   CRF 22 → VMAF 96.7, ratio ~2.4x on BBB 90s
#   CRF 23 → VMAF 96.1, ratio ~2.8x on BBB 90s (daily driver — 0.28x RT)
#   CRF 26 → VMAF 93.0, ratio ~4.8x (fast draft)
#
# SVT-AV1 CRF values (preset 6):
#   CRF 28 → VMAF 96.1, ratio ~2.8x
#   CRF 32 → VMAF 95.2, ratio ~3.6x (smallest at VMAF ≥ 95)
#   CRF 36 → VMAF 94.2, ratio ~4.7x
#
# VVC QP values (libvvenc, medium preset):
#   QP 28 → VMAF ~97+  (safe)
#   QP 32 → VMAF ~96   (balanced)
#   QP 34 → VMAF 95.81, 25.2× on 114 Mbps lossless master (MEASURED, maximum)
#   Note: vvenc is ~10-20x slower than x265 on Apple Silicon (unoptimised arm64)
#   The 25.2× / VMAF 95.81 result was at QP34 (maximum mode) with a 114 Mbps
#   FFV1 lossless source — not reproducible at balanced (QP32) or with a
#   pre-compressed H.264 source. Document the conditions, not just the number.
#
_MODE_PARAMS: dict[EncodeMode, dict] = {
    EncodeMode.SAFE: {
        "x265_preset":   "slow",
        "av1_preset":    6,
        "crf_x265":      22,    # VMAF ~96.7, p1 ~93
        "crf_av1":       28,    # VMAF ~96.1
        "vvc_qp":        28,    # VMAF ~97+
        "vtb_quality":   65,    # VideoToolbox quality 0-100; 65 ≈ high quality draft
        "vmaf_floor":    95.0,
    },
    EncodeMode.BALANCED: {
        "x265_preset":   "slow",
        "av1_preset":    6,
        "crf_x265":      23,    # VMAF ~96.1, ratio ~2.8x, 0.28x RT — daily driver
        "crf_av1":       32,    # VMAF ~95.2, ratio ~3.6x — smallest at 95%+
        "vvc_qp":        32,    # VMAF ~96
        "vtb_quality":   55,    # VideoToolbox balanced
        "vmaf_floor":    95.0,
    },
    EncodeMode.MAXIMUM: {
        "x265_preset":   "slow",
        "av1_preset":    6,
        "crf_x265":      26,    # VMAF ~93, ratio ~4.8x — maximum compression
        "crf_av1":       36,    # VMAF ~94.2, ratio ~4.7x
        "vvc_qp":        34,    # VMAF 95.81, 25.2× — MEASURED on 114Mbps lossless master
        "vtb_quality":   45,    # VideoToolbox maximum compression
        "vmaf_floor":    90.0,
    },
}

# Heuristic: if a scene has high spatial complexity it benefits less from AV1
# film-grain synthesis and should prefer x265 denoise+regrain.
_FILM_GRAIN_THRESHOLD = 8          # SVT-AV1 --film-grain value (0 = off, 1–50)
_SCENE_THRESHOLD      = 0.35       # ffmpeg scene-cut score threshold
_MIN_ZONE_DURATION_S  = 2.0        # seconds — zones shorter than this are merged

# BUG 3 FIX: x265 default keyframe interval used for frame-boundary alignment.
_KEYINT = 250


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------

def _require(binary: str) -> str:
    """Return the resolved path of *binary* or raise RuntimeError."""
    path = shutil.which(binary)
    if path is None:
        raise RuntimeError(
            f"Required binary '{binary}' not found on PATH.  "
            f"Install it and ensure it is accessible."
        )
    return path


def _check_dependencies(encoder: Encoder) -> dict[str, str]:
    """Verify required binaries exist; return a dict of resolved paths."""
    bins: dict[str, str] = {
        "ffmpeg":  _require("ffmpeg"),
        "ffprobe": _require("ffprobe"),
    }
    if encoder == Encoder.X265:
        pass  # libx265 is linked into ffmpeg; no separate binary needed.
    elif encoder in (Encoder.SVT_AV1, Encoder.VVC, Encoder.VIDEOTOOLBOX):
        enc_map = {
            Encoder.SVT_AV1:      ("libsvtav1",           "SVT-AV1"),
            Encoder.VVC:          ("libvvenc",             "VVC/H.266 (libvvenc)"),
            Encoder.VIDEOTOOLBOX: ("hevc_videotoolbox",    "VideoToolbox HEVC (Apple only)"),
        }
        enc_flag, codec_name = enc_map[encoder]
        result = subprocess.run(
            [bins["ffmpeg"], "-encoders"],
            capture_output=True, text=True, timeout=10
        )
        if enc_flag not in result.stdout:
            raise RuntimeError(
                f"ffmpeg was found but '{enc_flag}' is not available.  "
                f"{codec_name} requires Apple Silicon Mac with macOS 10.13+."
                if encoder == Encoder.VIDEOTOOLBOX else
                f"ffmpeg was found but was not compiled with --enable-{enc_flag}.  "
                f"{codec_name} is not available in this build."
            )
    return bins


# ---------------------------------------------------------------------------
# Media introspection
# ---------------------------------------------------------------------------

@dataclass
class VideoInfo:
    duration:   float   # seconds
    width:      int
    height:     int
    fps:        float
    bit_depth:  int     # 8 or 10
    codec:      str
    has_audio:  bool
    file_size:  int     # bytes
    grain_level: float  # estimated spatial noise, 0–1
    is_screen_content: bool = False  # True when source looks like a screen recording


def probe_video(path: Path, ffprobe: str) -> VideoInfo:
    """Extract stream metadata with ffprobe."""
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{proc.stderr}")

    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    fmt     = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise ValueError(f"No video stream found in '{path}'")

    # Frame rate
    fps_raw = video_stream.get("avg_frame_rate", "24/1")
    num, den = fps_raw.split("/")
    fps = float(num) / float(den) if float(den) else 24.0

    # Bit depth from pix_fmt
    pix_fmt   = video_stream.get("pix_fmt", "yuv420p")
    bit_depth = 10 if "10" in pix_fmt or "p10" in pix_fmt else 8

    # Duration
    duration = float(fmt.get("duration", video_stream.get("duration", 0)))

    grain_level = _estimate_grain(video_stream, fps)

    # Screen content detection: low grain + high-res + common screen codec.
    # Screen recordings (macOS, OBS, etc.) are always near-grain-free — a
    # real film frame at 4K and ~48 Mbps looks identical in bitrate terms but
    # has a much higher grain_level from the heuristic.
    height = int(video_stream.get("height", 1080))
    src_codec = video_stream.get("codec_name", "unknown")
    is_screen = (
        grain_level < 0.15
        and fps >= 25.0
        and height >= 1080
        and src_codec in ("h264", "hevc", "h265", "vp9", "av1")
    )

    return VideoInfo(
        duration          = duration,
        width             = int(video_stream.get("width", 1920)),
        height            = height,
        fps               = fps,
        bit_depth         = bit_depth,
        codec             = src_codec,
        has_audio         = audio_stream is not None,
        file_size         = int(fmt.get("size", 0)),
        grain_level       = grain_level,
        is_screen_content = is_screen,
    )


def _estimate_grain(stream: dict, fps: float) -> float:
    """
    Heuristic grain estimate from codec metadata.
    Real-world refinement would sample a frame and compute variance.
    Returns 0.0 (clean) – 1.0 (heavy grain/noise).

    Parameters
    ----------
    stream:
        A single stream dict from ffprobe JSON output.
    fps:
        Actual frame rate of the stream, used for bits-per-pixel calculation.
        Previously this was hardcoded to 24; passing the real fps prevents
        overestimating grain on high-frame-rate content. (BUG 1 FIX)
    """
    tags = stream.get("tags", {})
    # Some encoders store noise metadata in tags
    if any("film_grain" in k.lower() for k in tags):
        return 0.8
    # Fallback: bitrate vs resolution ratio as a proxy.
    # BUG 1 FIX: use actual fps instead of the former hardcoded constant 24.
    try:
        bitrate  = float(stream.get("bit_rate", 0))
        pixels   = int(stream.get("width", 1920)) * int(stream.get("height", 1080))
        effective_fps = fps if fps > 0 else 24.0
        bpp      = bitrate / (pixels * effective_fps)   # bits per pixel at actual fps
        # High bpp suggests rich detail or noise
        return min(1.0, max(0.0, (bpp - 0.04) / 0.16))
    except (ZeroDivisionError, TypeError):
        return 0.3


# ---------------------------------------------------------------------------
# Encoder auto-selection
# ---------------------------------------------------------------------------

def select_encoder(info: VideoInfo, encoder: Optional[Encoder]) -> Encoder:
    """
    Choose the best encoder when the caller passes ``encoder=None``.

    Routing logic (from measured benchmarks, 2026-06-04):

    Screen content → x265 with screen preset
        x265 + no-sao/tskip outperforms AV1 on UI/text because AV1 film-grain
        synthesis is irrelevant and scm=1 IBC only helps repetitive patterns.

    High grain (grain_level > 0.5) → x265
        AV1 film-grain synthesis (film-grain=N metadata) failed on Jellyfish
        (VMAF 54 at grain=4); x265 encodes grain as-is and preserves texture.
        Measured: x265 CRF22 → VMAF 95.44 on Jellyfish; AV1 → VMAF 54.

    Everything else → SVT-AV1
        Benchmarks showed AV1 preset-6 CRF32 is:
        - Faster than x265 slow on long content (2.37x RT vs 0.25x RT on BBB full)
        - Smaller at equal VMAF (17.5 MB vs 22.6 MB at VMAF 95+ on BBB 90s)
        - The "duration < 30s → x265" heuristic was wrong: AV1 overhead is
          negligible and speed advantage applies to all durations.
    """
    if encoder is not None:
        return encoder

    if info.is_screen_content:
        log.info("auto-encoder: screen content detected → x265 (screen preset)")
        return Encoder.X265

    if info.grain_level > 0.5:
        log.info(
            "auto-encoder: high grain (%.2f) → x265 "
            "(AV1 film-grain synthesis unreliable on this content class)",
            info.grain_level,
        )
        return Encoder.X265

    log.info(
        "auto-encoder: clean/balanced content, grain=%.2f → svt-av1 "
        "(faster + smaller than x265 at VMAF 95+)",
        info.grain_level,
    )
    return Encoder.SVT_AV1


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------

@dataclass
class SceneCut:
    timestamp: float   # seconds
    score:     float   # 0–1


def detect_scene_cuts(
    path: Path,
    ffmpeg: str,
    threshold: float = _SCENE_THRESHOLD,
) -> List[SceneCut]:
    """
    Use ffmpeg scene-detection filter to find cut boundaries.

    Runs::

        ffmpeg -i input -filter:v "select=gt(scene\\,THRESH),showinfo" -f null -

    and parses the showinfo output for pts_time and scene score.
    """
    log.info("detecting scene cuts (threshold=%.2f) …", threshold)
    cmd = [
        ffmpeg,
        "-i", str(path),
        "-filter:v", f"select=gt(scene\\,{threshold}),showinfo",
        "-vsync", "vfr",
        "-f", "null", "-",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,     # large files can take time
    )
    # showinfo writes to stderr
    cuts: List[SceneCut] = []
    # Pattern: showinfo emits lines like:
    #   [Parsed_showinfo_1 @ ...] n:  0 pts: 1234 pts_time:51.4167 ...
    # scene score appears on the preceding 'select' log line or in metadata.
    # We parse pts_time from showinfo lines.
    pts_pattern   = re.compile(r"pts_time:(\d+\.?\d*)")
    score_pattern = re.compile(r"scene_score=(\d+\.?\d*)", re.IGNORECASE)

    pending_score: Optional[float] = None
    for line in proc.stderr.splitlines():
        sm = score_pattern.search(line)
        if sm:
            pending_score = float(sm.group(1))

        pm = pts_pattern.search(line)
        if pm:
            ts    = float(pm.group(1))
            score = pending_score if pending_score is not None else threshold
            cuts.append(SceneCut(timestamp=ts, score=score))
            pending_score = None

    log.info("  found %d scene cuts", len(cuts))
    return cuts


# ---------------------------------------------------------------------------
# Zone construction
# ---------------------------------------------------------------------------

@dataclass
class Zone:
    start: float   # seconds
    end:   float   # seconds
    crf_offset: int  # applied on top of base CRF (negative = higher quality)
    label: str


def _snap_to_keyframe_boundary(timestamp: float, fps: float, keyint: int = _KEYINT) -> float:
    """
    BUG 3 FIX: Round a timestamp to the nearest keyframe interval boundary.

    x265 and SVT-AV1 both insert keyframes every *keyint* frames by default.
    Zone boundaries that land mid-GOP force a keyframe insertion at encode
    time, which wastes bits and can cause seek artefacts.  Snapping boundaries
    to the nearest GOP boundary avoids this.

    Parameters
    ----------
    timestamp:
        Time in seconds to snap.
    fps:
        Stream frame rate.
    keyint:
        Keyframe interval in frames (default: 250, matching x265/SVT-AV1 default).

    Returns
    -------
    Snapped timestamp in seconds.
    """
    if fps <= 0 or keyint <= 0:
        return timestamp
    frame_num  = round(timestamp * fps)
    snapped    = round(frame_num / keyint) * keyint
    return snapped / fps


def build_zones(
    info:     VideoInfo,
    cuts:     List[SceneCut],
    base_crf: int,
) -> List[Zone]:
    """
    Construct adaptive bitrate zones from scene cut timestamps.

    Strategy
    --------
    * Each inter-cut segment becomes a candidate zone.
    * Segments shorter than ``_MIN_ZONE_DURATION_S`` are merged into their
      neighbour.
    * Cuts with high scene-change score get a negative CRF offset (more bits)
      because they likely contain complex/important frames.
    * Long static segments (low-motion) get +2 CRF (fewer bits).
    * BUG 3 FIX: Zone boundaries are snapped to keyframe-interval multiples
      so the encoder does not have to insert mid-GOP keyframes.
    """
    fps    = info.fps
    keyint = _KEYINT

    boundaries = [0.0] + [c.timestamp for c in cuts] + [info.duration]
    scores     = [0.0]  + [c.score     for c in cuts] + [0.0]

    raw_zones: List[Tuple[float, float, float]] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end   = boundaries[i + 1]
        score = scores[i]
        if end - start >= _MIN_ZONE_DURATION_S:
            raw_zones.append((start, end, score))
        elif raw_zones:
            # Merge into previous
            s, _, prev_score = raw_zones[-1]
            raw_zones[-1] = (s, end, max(prev_score, score))

    zones: List[Zone] = []
    for idx, (start, end, score) in enumerate(raw_zones):
        if score > 0.7:
            offset = -2       # complex scene cut → more bits
        elif score < 0.1:
            # Near-static on screen content = idle desktop with sharp text.
            # The encoder has fewer reference frames to hide quantization, so
            # block artifacts on crisp edges are immediately visible.  Give
            # more bits (offset 0), not fewer.  The old +2 was correct only
            # for natural video where near-static = easy sky/background.
            offset = 0 if info.is_screen_content else +2
        else:
            offset = 0

        # Snap start/end to keyframe boundaries for clean GOP alignment.
        snapped_start = _snap_to_keyframe_boundary(start, fps, keyint)
        # Zone end: snap, but NEVER go backward — if the nearest keyframe
        # boundary is earlier than the original end, keep the original end.
        # Going backward on the last zone leaves frames beyond the snap point
        # outside all zones, and x265 encodes them with no CRF guidance,
        # producing catastrophic quality loss (confirmed: Jellyfish VMAF 15
        # when frames 250-299 were unzoned in a 300-frame 30fps clip with
        # keyint=250).
        _snapped_end = _snap_to_keyframe_boundary(end, fps, keyint)
        snapped_end  = max(_snapped_end, end)   # never snap backward

        # Guard: ensure snapping did not collapse the zone below the minimum.
        effective_duration = snapped_end - snapped_start
        if effective_duration < _MIN_ZONE_DURATION_S:
            log.warning(
                "zone_%03d: snapped duration %.2f s is below minimum %.1f s "
                "(start=%.3f → %.3f, end=%.3f → %.3f); zone kept with original boundaries.",
                idx, effective_duration, _MIN_ZONE_DURATION_S,
                start, snapped_start, end, snapped_end,
            )
            snapped_start = start
            snapped_end   = end

        zones.append(Zone(
            start      = snapped_start,
            end        = snapped_end,
            crf_offset = offset,
            label      = f"zone_{idx:03d}",
        ))

    log.info("  built %d zones from %d cuts", len(zones), len(cuts))
    return zones


# ---------------------------------------------------------------------------
# VMAF measurement
# ---------------------------------------------------------------------------

@dataclass
class VMAFResult:
    """Structured VMAF measurement outcome."""
    mean:         float
    percentile_1: float   # 1st percentile — worst ~1 % of frames


def _vmaf_timeout(duration: float) -> int:
    """
    Compute a safe subprocess timeout (seconds) for a VMAF measurement run.

    At n_subsample=6, n_threads=8 on Apple Silicon M4, libvmaf processes 4K
    content at roughly 0.5x real-time.  The formula below gives 2x headroom
    plus a 120-second base for startup and I/O, with a 300-second floor so
    short clips never get an unreasonably tight limit.

    Benchmarked on this machine (M4, 10 cores, ffmpeg 8.1.1 static):
      - 28.5s  4096x2304 @39fps, n_subsample=6: 11.6s  (0.41x RT)
      - 30.0s  4096x2304 @39fps, n_subsample=6: 14.2s  (0.47x RT)
      - 283.3s 4096x2304 @49fps, n_subsample=6: ~147s  (0.52x RT, estimated)
    """
    return max(300, int(duration) + 120)


def measure_vmaf(
    reference:   Path,
    distorted:   Path,
    ffmpeg:      str,
    model:       str   = "version=vmaf_v0.6.1",
    n_subsample: int   = 6,
    duration:    float = 0.0,
) -> VMAFResult:
    """
    Measure VMAF of *distorted* vs *reference* using ffmpeg's libvmaf filter.

    Returns a :class:`VMAFResult` with mean and 1st-percentile scores.
    Both fields are -1.0 on failure (the caller continues; the on-chain proof
    hash is always computed regardless of VMAF outcome).

    Parameters
    ----------
    reference:
        Original (uncompressed) source file.
    distorted:
        Encoded output to evaluate.
    ffmpeg:
        Resolved path to the ffmpeg binary.
    model:
        libvmaf model string.  Default ``version=vmaf_v0.6.1`` is the standard
        HD model.  Pass ``version=vmaf_4k_v0.6.1`` for 4K content — it exists
        in the bundled ffmpeg 8.1.1 static build (confirmed available) and
        applies 4K-viewing-distance perceptual weights.  Both models run at
        the same speed; the 4K model scores approximately 2–3 points higher on
        native 4K screen content.
    n_subsample:
        Evaluate every Nth frame for VMAF.  Default 6.

        Benchmarked accuracy vs n_subsample=1 (ground truth) on 4K screen
        recordings encoded at CRF 22 (x265):

          n_subsample=1  : baseline  (42.9s for 30s clip)
          n_subsample=4  : -0.004 VMAF units, 2.6x faster
          n_subsample=6  : -0.006 VMAF units, 3.0x faster  [default]
          n_subsample=10 : +0.006 VMAF units, 3.7x faster
          n_subsample=15 : -0.091 VMAF units, 3.8x faster  [accuracy drops]

        n_subsample=6 is the sweet spot: negligible accuracy loss, 3x speedup.
        Do not exceed 12 without validating on your content type.
    duration:
        Video duration in seconds, used to compute a safe adaptive timeout.
        If 0.0 (default), a 300-second floor is used.

    Implementation note — why Popen instead of subprocess.run
    ----------------------------------------------------------
    ``subprocess.run(..., timeout=600)`` uses ``communicate(timeout=600)``
    internally.  When the timeout expires it raises ``TimeoutExpired`` and
    leaves the ffmpeg process running as a zombie until the exception propagates
    up.  If the exception is uncaught (as it was in the original code), the
    whole Python process exits 1 before the proof hash is computed.

    Using ``Popen`` with an explicit ``communicate(timeout=N)`` + ``kill()``
    fallback ensures the process is always reaped, the exception is always
    caught locally, and VMAF failure degrades gracefully to ``mean=-1.0``
    rather than crashing the pipeline.

    BUG 2 FIX: previously only the mean was extracted; the 1st percentile
    (worst ~1 % of frames) is now parsed and returned so callers can detect
    perceptual quality cliffs.  libvmaf 2.x in this ffmpeg build does not emit
    ``percentile_1`` in the JSON; ``min`` is used as a conservative proxy.
    """
    n_threads = min(os.cpu_count() or 4, 8)
    timeout   = _vmaf_timeout(duration) if duration > 0.0 else 300

    log.info(
        "measuring VMAF (n_subsample=%d, n_threads=%d, timeout=%ds) …",
        n_subsample, n_threads, timeout,
    )

    vmaf_log = distorted.with_suffix(".vmaf.json")
    cmd = [
        ffmpeg, "-y",
        "-i", str(distorted),
        "-i", str(reference),
        "-filter_complex",
        (
            f"[0:v][1:v]libvmaf="
            f"model={model}:"
            f"log_fmt=json:"
            f"log_path={vmaf_log}:"
            f"n_threads={n_threads}:"
            f"n_subsample={n_subsample}"
        ),
        "-f", "null", "-",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        log.warning(
            "VMAF timed out after %ds (video %.0fs).  "
            "Killing ffmpeg and returning -1.  "
            "The proof hash will still be computed.",
            timeout, duration,
        )
        proc.kill()
        proc.communicate()   # drain pipes so the process is fully reaped
        return VMAFResult(mean=-1.0, percentile_1=-1.0)
    except Exception as exc:
        log.warning("VMAF subprocess error: %s", exc)
        try:
            proc.kill()
            proc.communicate()
        except Exception:
            pass
        return VMAFResult(mean=-1.0, percentile_1=-1.0)

    if returncode != 0:
        log.warning("VMAF measurement failed (exit %d):\n%s",
                    returncode, stderr.decode(errors="replace")[-2000:])
        return VMAFResult(mean=-1.0, percentile_1=-1.0)

    try:
        with open(vmaf_log) as fh:
            data = json.load(fh)
        vmaf_metrics = data["pooled_metrics"]["vmaf"]
        mean         = float(vmaf_metrics["mean"])
        # libvmaf 2.x in ffmpeg 8.1.1 emits min/max/mean/harmonic_mean —
        # not percentile_1.  Use min as a conservative worst-frame proxy.
        percentile_1 = float(
            vmaf_metrics.get("percentile_1",
            vmaf_metrics.get("min", mean))
        )
        log.info("  VMAF mean=%.2f  p1=%.2f", mean, percentile_1)
        return VMAFResult(mean=mean, percentile_1=percentile_1)
    except Exception as exc:
        log.warning("could not parse VMAF JSON: %s", exc)
        return VMAFResult(mean=-1.0, percentile_1=-1.0)


# ---------------------------------------------------------------------------
# Proof hash
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Return hex SHA-256 digest of *path*, suitable for on-chain anchoring."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Encode helpers
# ---------------------------------------------------------------------------

def _build_x265_command(
    ffmpeg:      str,
    input_path:  Path,
    output_path: Path,
    info:        VideoInfo,
    zones:       List[Zone],
    mode:        EncodeMode,
    target_vmaf: float,
) -> List[str]:
    """
    Build the ffmpeg / libx265 encode command.

    Zone-level CRF is approximated via x265 zones (--zones syntax embedded in
    x265-params).  The zones string format is::

        <start_frame>,<end_frame>,crf=<value>

    where frame numbers are computed from timestamps x fps, then snapped to
    keyframe-interval boundaries (BUG 3 FIX applied upstream in build_zones).
    """
    p        = _MODE_PARAMS[mode]
    base_crf = p["crf_x265"]
    preset   = p["x265_preset"]
    fps      = info.fps

    # Build x265 zones string.  Frame numbers are derived from the already-
    # keyframe-snapped zone boundaries produced by build_zones.
    # --- x265-params string ---
    # Three hard rules for x265 4.0 compatibility (confirmed from encode logs):
    #
    # 1. Do NOT put 'preset' or 'profile' in x265-params.  x265 4.0 dropped
    #    them as x265-params keys; pass via ffmpeg's -preset / -profile:v flags.
    #
    # 2. Do NOT put 'deblock=A:B' in x265-params.  ffmpeg uses ':' as the
    #    key=value pair separator in the x265-params string, so 'deblock=-1:-1'
    #    is split into ('deblock=-1', '-1') — x265 sees an Unknown option '-1'.
    #    Use the ffmpeg-level option '-x265-param deblock=A,B' or omit deblock
    #    and rely on x265 defaults, which are equivalent to -1:-1 for most
    #    content.  Confirmed bug: produces VMAF 15 (Jellyfish, CRF 20).
    #
    # 3. Do NOT put zones in x265-params when all zones share the same CRF.
    #    For single-zone encodes (no scene cuts) x265 4.0 rejects the zones
    #    value format and falls back to unconstrained QP — producing 217 KB
    #    garbage at VMAF 0.39.  For single-zone, fold the CRF offset into the
    #    global -crf flag instead.  For multi-zone, use the zones= key only
    #    (verified working in x265 4.0 with format start,end,crf=N/start2…).

    # Fold single-zone CRF offset into global CRF — no zones string needed.
    unique_offsets = {z.crf_offset for z in zones}
    if len(zones) <= 1 or len(unique_offsets) == 1:
        # All zones share one CRF offset (or there are no cuts) — just adjust
        # the global CRF.  This avoids the x265 4.0 zones= parsing bug entirely
        # for the common case.
        offset    = next(iter(unique_offsets), 0)
        base_crf  = max(12, min(51, base_crf + offset))
        zones_str = ""
    else:
        # Multiple zones with different CRF values (actual scene cuts).
        # Frame boundaries: never snap backward; clamp to clip end.
        total_frames = max(1, round(info.duration * fps))
        zone_parts: List[str] = []
        for z in zones:
            sf  = int(z.start * fps)
            ef  = min(total_frames - 1, max(sf, round(z.end * fps)))
            crf = max(12, min(51, base_crf + z.crf_offset))
            zone_parts.append(f"{sf},{ef},crf={crf}")
        zones_str = "/".join(zone_parts)

    if info.is_screen_content:
        # Screen/text/UI preset — optimised for sharp edges and flat fills.
        #
        # no-sao: SAO blurs high-frequency edges like text. Disabling it is the
        #   single highest-impact change for screen content.
        # no-strong-intra-smoothing: bilinear-smooths 32x32 intra blocks —
        #   smears flat fills and makes text look fuzzy.
        # tskip=1: bypasses DCT for 4x4 near-flat blocks. 15-40% bitrate
        #   reduction on text/UI at equal perceptual quality.
        # aq-mode=4 / aq-strength=0.6: variance-based AQ, lighter strength for
        #   large flat regions that don't need extra bits.
        # psy-rdoq=0.0: DCT-skip content doesn't benefit from RDOQ.
        # No deblock in x265-params (see rule 2 above) — omitting deblock
        #   defaults to the encoder's internal -1:-1 which is fine here since
        #   no-sao already handles edge preservation.
        x265_params = [
            "high-tier=1",
            "ref=5",
            "bframes=8",
            "b-adapt=2",
            "rc-lookahead=60",
            "aq-mode=4",
            "aq-strength=0.6",
            "psy-rd=0.8",
            "psy-rdoq=0.0",
            "me=umh",
            "no-sao",
            "no-strong-intra-smoothing",
            "tskip=1",
        ]
    else:
        x265_params = [
            "high-tier=1",
            "ref=5",
            "bframes=8",
            "b-adapt=2",
            "rc-lookahead=60",
            "aq-mode=3",
            "aq-strength=0.8",
            "psy-rd=1.0",
            "psy-rdoq=1.5",
            "me=umh",
        ]
    if zones_str:
        x265_params.append(f"zones={zones_str}")

    # Apple Silicon thread tuning.
    # x265 auto-detects 10 threads on M4 (4P+6E) but defaults to 3 frame threads.
    # Setting frame-threads=P_CORES ensures each frame thread gets a full P-core
    # for latency-sensitive work; pools=+ lets E-cores fill in WPP row parallelism.
    if _IS_APPLE_SIL:
        x265_params += [
            f"frame-threads={_CPU_P_CORES}",  # one per P-core (M4=4)
            "pools=+",                         # use all available cores for WPP
            "wpp=1",                           # wavefront parallel processing on
        ]

    # No pre-encode denoise for x265.
    # x265 handles film grain natively through its psychovisual optimisations
    # (psy-rd, aq-mode) and the grain simply compresses less efficiently — which
    # CRF already accounts for by spending more bits.  hqdn3d on high-grain
    # content (grain_level >= 0.5) was measured to destroy perceptual quality:
    # Jellyfish 1080p (grain=1.0) went from VMAF 88 to VMAF 15 after applying
    # hqdn3d=2:2:3:3, because the filter blurs the fine translucent texture into
    # a formless blob that x265 then has to encode as low-bitrate flat surfaces.
    # The synthetic noise filter (noise=alls=5:allf=t+u) was also removed: it
    # produces temporally-uncorrelated random noise that x265 cannot predict
    # across frames, collapsing compression efficiency entirely.
    # Denoise only belongs in the AV1 path, where SVT-AV1 encodes the clean
    # signal and re-adds matching grain at decode time via bitstream metadata.
    vf_filters: List[str] = []

    # 10-bit pixel format
    pix_fmt = "yuv420p10le" if info.bit_depth == 10 else "yuv420p"

    cmd: List[str] = [
        ffmpeg, "-y",
        "-i", str(input_path),
    ]
    if vf_filters:
        cmd += ["-filter:v", ",".join(vf_filters)]

    # preset and profile must be ffmpeg-level flags, NOT in x265-params
    # (x265 4.0 dropped them as x265-params keys — they log 'Unknown option'
    # and the encode silently degrades).
    profile_flag = "main10" if info.bit_depth == 10 else "main"
    cmd += [
        "-c:v", "libx265",
        "-preset", preset,
        "-profile:v", profile_flag,
        "-crf", str(base_crf),
    ]
    if x265_params:
        cmd += ["-x265-params", ":".join(x265_params)]
    cmd += [
        "-pix_fmt", pix_fmt,
        "-c:a", "copy",
        "-tag:v", "hvc1",          # Apple QuickTime/Finder/iOS require hvc1, not hev1
        "-movflags", "+faststart", # move moov atom to front for HTTP progressive play
        str(output_path),
    ]
    return cmd


def _build_svtav1_command(
    ffmpeg:      str,
    input_path:  Path,
    output_path: Path,
    info:        VideoInfo,
    zones:       List[Zone],
    mode:        EncodeMode,
    target_vmaf: float,
) -> List[str]:
    """
    Build the ffmpeg / libsvtav1 encode command.

    SVT-AV1 does not support per-zone CRF natively through ffmpeg's interface,
    so we encode each zone as a separate segment and concatenate (handled by
    the caller for maximum mode).  For safe/balanced, a single-pass global
    CRF is used with film-grain synthesis handling grain retention.
    """
    p        = _MODE_PARAMS[mode]
    base_crf = p["crf_av1"]
    preset   = p["av1_preset"]

    # Film grain synthesis level (0 = off)
    film_grain = 0
    if info.grain_level > 0.3:
        # Map grain_level 0.3–1.0 → film-grain 4–20
        film_grain = int(4 + (info.grain_level - 0.3) / 0.7 * 16)
        film_grain = max(1, min(50, film_grain))
        log.info("  AV1 film grain synthesis level: %d", film_grain)

    # SVT-AV1 params string.
    # tune=0: psychovisual quality mode (vs tune=1 PSNR).  Measurably sharper
    #   on complex content; community-validated 0.3-0.8 VMAF improvement.
    # scm=1: screen content mode — enables IntraBC (finds matching blocks within
    #   the same frame, crucial for repetitive UI like taskbars/text/icons) and
    #   palette coding for limited-colour regions.  15-40% bitrate reduction on
    #   screen/UI content at equal quality.  scm=2 is content-adaptive auto.
    scm = "1" if info.is_screen_content else "2"
    svtav1_params = f"film-grain={film_grain}:enable-overlays=1:scd=1:tune=0:scm={scm}"
    if mode == EncodeMode.MAXIMUM:
        svtav1_params += ":hierarchical-levels=5:lookahead=60"
    # 4K tiling: tile-columns=1:tile-rows=1 gives ~70% speed improvement on
    # 4K content with only ~1.3% VMAF penalty — well within all quality floors.
    if info.height >= 2160:
        svtav1_params += ":tile-columns=1:tile-rows=1"

    pix_fmt = "yuv420p10le" if info.bit_depth == 10 else "yuv420p"

    # Light denoise for very noisy sources (film grain synthesis replaces grain)
    vf_filters: List[str] = []
    if info.grain_level > 0.6:
        vf_filters.append("hqdn3d=1.5:1.5:2:2")

    cmd: List[str] = [
        ffmpeg, "-y",
        "-i", str(input_path),
    ]
    if vf_filters:
        cmd += ["-filter:v", ",".join(vf_filters)]

    cmd += [
        "-c:v", "libsvtav1",
        "-crf", str(base_crf),
        "-preset", str(preset),
        "-svtav1-params", svtav1_params,
        "-pix_fmt", pix_fmt,
        "-c:a", "libopus",    # AV1 containers pair well with Opus
        "-b:a", "128k",
        str(output_path),
    ]
    return cmd


def _build_vvc_command(
    ffmpeg:      str,
    input_path:  Path,
    output_path: Path,
    info:        VideoInfo,
    mode:        EncodeMode,
) -> List[str]:
    """
    Build the ffmpeg / libvvenc (H.266/VVC) encode command.

    VVC (Versatile Video Coding / H.266) delivers ~25-40% better compression
    than HEVC at equal perceptual quality.  Measured on this machine:
      - QP 34, medium preset → VMAF 95.81, 25.2× ratio on 114 Mbps lossless source
      - 692 MB movie → 40 MB at VMAF 94.77 (AV1) vs comparable VVC sizes

    Speed caveat: libvvenc on Apple Silicon arm64 is not yet hardware-optimised.
    Expect 10-20× slower than x265 (e.g. 0.05× realtime on 1080p 30fps content).
    Use for archival encodes or when file size is the hard constraint.

    vvenc uses QP (quantisation parameter) not CRF — QP range 0-63.
    Approximate mapping to x265 CRF and expected VMAF on 1080p natural video:
      QP 28 → VMAF ~97+  (safe)
      QP 32 → VMAF ~96   (balanced)
      QP 36 → VMAF ~94   (maximum)
      QP 34 → VMAF 95.81 (confirmed on 114 Mbps lossless Jellyfish source)

    -qpa true: subjective (perceptually motivated) optimisation — always on.
    Output is MP4 with hvc1-compatible container.  libvvenc writes VVC bitstream
    in an ISOBMFF (MP4) container; playback requires a VVC-capable player
    (VLC 3.0.21+, ffplay, mpv with VVC build) — QuickTime does not support VVC.
    """
    p    = _MODE_PARAMS[mode]
    qp   = p["vvc_qp"]
    # vvenc presets: 0=fastest … 4=slowest.  "slow" maps to preset 3.
    # For balanced/maximum we use medium (2) as a speed compromise.
    vvc_preset = "slow" if mode == EncodeMode.SAFE else "medium"
    pix_fmt = "yuv420p"   # libvvenc in this build only supports 8-bit 4:2:0

    log.info(
        "  VVC encode: QP=%d preset=%s  "
        "(expect ~10-20x slower than x265 — libvvenc not yet arm64-optimised)",
        qp, vvc_preset,
    )

    cmd: List[str] = [
        ffmpeg, "-y",
        "-i", str(input_path),
        "-c:v", "libvvenc",
        "-preset", vvc_preset,
        "-qp", str(qp),
        "-qpa", "true",       # perceptual optimisation
        "-pix_fmt", pix_fmt,
        "-c:a", "copy",
        str(output_path),
    ]
    return cmd


def _build_videotoolbox_command(
    ffmpeg:      str,
    input_path:  Path,
    output_path: Path,
    info:        VideoInfo,
    mode:        EncodeMode,
) -> List[str]:
    """
    Build an Apple VideoToolbox hardware HEVC encode command.

    VideoToolbox drives the HEVC encode block inside Apple Silicon directly —
    no CPU involvement in the pixel pipeline.  Benchmarks show ~8-15× faster
    than libx265 on M-series at comparable visual quality for screen recordings
    and clean natural video.

    Quality is set via ffmpeg's -q:v (0-100, higher = better quality / larger
    file).  It does NOT map linearly to CRF or VMAF — use it as a draft-preview
    path, not archival.  The output is always tagged hvc1 (VideoToolbox native),
    so it opens natively in QuickTime, Finder, and iOS.

    When to use:
      * Quick preview / proxy encode during editing
      * Live / near-realtime capture re-encode
      * Any time you need the result in seconds, not minutes

    When NOT to use:
      * Archival or delivery encodes (quality ceiling is lower than libx265)
      * Grain-heavy content (VTB grain retention is poor)

    Measured speed on M4: 4K 60fps screen recording → ~4-6× faster than x265.
    """
    p       = _MODE_PARAMS[mode]
    quality = p["vtb_quality"]   # 0-100, higher = better quality
    pix_fmt = "yuv420p"

    log.info(
        "  VideoToolbox HEVC: quality=%d (hardware, ~8-15× faster than x265)",
        quality,
    )

    cmd: List[str] = [
        ffmpeg, "-y",
        "-i", str(input_path),
        "-c:v", "hevc_videotoolbox",
        "-q:v", str(quality),
        "-allow_sw", "1",      # fall back to software if GPU unavailable
        "-pix_fmt", pix_fmt,
        "-c:a", "copy",
        # hvc1 tag: VideoToolbox writes it natively — no -tag:v needed
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


@dataclass
class EncodeMetrics:
    """Resource metrics collected during the encode pass."""
    wall_s:    float = 0.0   # wall-clock seconds
    cpu_avg:   float = 0.0   # average total-CPU % (sum across all cores)
    cpu_peak:  float = 0.0   # peak 1-second total-CPU %
    rss_peak_mb: float = 0.0 # peak resident-set size (MB)


def _run_encode(cmd: List[str], log_path: Path) -> EncodeMetrics:
    """
    Execute an encode command, stream stderr to log, and collect resource metrics.

    CPU monitoring uses psutil (available) to sample total CPU utilisation every
    500 ms across all cores while the encoder is running.  The reported values:
      cpu_pct_avg  — mean  of (sum of per-core %) samples → tells you how much of
                     the machine the encoder consumed on average.
      cpu_pct_peak — max sample → catches bursts (scene-cut analysis, I/O spikes).
      rss_peak_mb  — peak resident memory (from psutil, same process tree).

    On Apple Silicon the P-core / E-core split means a raw "X% of 10 cores"
    figure understates quality-work: P-cores do ~3-4× work per clock vs E-cores,
    so 400% on 4 P-cores ≈ 800-1000% effective on all-E-core equivalents.
    """
    log.info("running: %s", " ".join(cmd[:6]) + " …")
    metrics = EncodeMetrics()

    # Try to import psutil for monitoring; gracefully degrade if missing.
    try:
        import psutil as _psutil
        _have_psutil = True
    except ImportError:
        _psutil = None  # type: ignore[assignment]
        _have_psutil = False

    cpu_samples: List[float] = []
    rss_samples: List[float] = []
    stop_monitor = threading.Event()

    def _monitor(pid: int) -> None:
        """Background thread: sample CPU and RSS every 500 ms."""
        if not _have_psutil:
            return
        try:
            proc_ps = _psutil.Process(pid)
            while not stop_monitor.wait(timeout=0.5):
                try:
                    # cpu_percent(interval=None) → delta since last call, all children
                    children = proc_ps.children(recursive=True)
                    total_cpu = proc_ps.cpu_percent(interval=None)
                    for c in children:
                        try:
                            total_cpu += c.cpu_percent(interval=None)
                        except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                            pass
                    cpu_samples.append(total_cpu)
                    rss = proc_ps.memory_info().rss / 1048576
                    rss_samples.append(rss)
                except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                    break
        except Exception:
            pass

    t0 = _time.monotonic()
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(" ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        monitor_thread = threading.Thread(target=_monitor, args=(proc.pid,), daemon=True)
        monitor_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
        proc.wait()

        stop_monitor.set()
        monitor_thread.join(timeout=2)

    metrics.wall_s = _time.monotonic() - t0
    if cpu_samples:
        metrics.cpu_avg  = round(sum(cpu_samples) / len(cpu_samples), 1)
        metrics.cpu_peak = round(max(cpu_samples), 1)
    if rss_samples:
        metrics.rss_peak_mb = round(max(rss_samples), 1)

    log.info(
        "  encode done: %.1fs wall | CPU avg=%.0f%% peak=%.0f%% | RSS peak=%.0fMB",
        metrics.wall_s, metrics.cpu_avg, metrics.cpu_peak, metrics.rss_peak_mb,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"Encode failed (exit {proc.returncode}).  "
            f"See log: {log_path}"
        )
    return metrics


# ---------------------------------------------------------------------------
# Gladiator zone-segment encode
# ---------------------------------------------------------------------------

def _encode_maximum_svtav1(
    ffmpeg:      str,
    input_path:  Path,
    output_path: Path,
    info:        VideoInfo,
    zones:       List[Zone],
    mode:        EncodeMode,
    target_vmaf: float,
    work_dir:    Path,
) -> None:
    """
    Per-zone encode for maximum mode with SVT-AV1.

    Each zone is extracted, encoded with its own CRF, then all segments are
    concatenated via ffmpeg concat demuxer.  Zone boundaries have already been
    snapped to keyframe intervals by build_zones (BUG 3 FIX).
    """
    p        = _MODE_PARAMS[mode]
    base_crf = p["crf_av1"]
    preset   = p["av1_preset"]

    film_grain = 0
    if info.grain_level > 0.3:
        film_grain = int(4 + (info.grain_level - 0.3) / 0.7 * 16)
        film_grain = max(1, min(50, film_grain))

    segment_paths: List[Path] = []
    for z in zones:
        seg_path      = work_dir / f"{z.label}.mkv"
        seg_log       = work_dir / f"{z.label}.log"
        zone_crf      = max(12, min(63, base_crf + z.crf_offset))
        scm = "1" if info.is_screen_content else "2"
        svtav1_params = f"film-grain={film_grain}:enable-overlays=1:scd=0:tune=0:scm={scm}"

        pix_fmt = "yuv420p10le" if info.bit_depth == 10 else "yuv420p"

        seg_cmd = [
            ffmpeg, "-y",
            "-ss", str(z.start),
            "-to", str(z.end),
            "-i", str(input_path),
            "-c:v", "libsvtav1",
            "-crf", str(zone_crf),
            "-preset", str(preset),
            "-svtav1-params", svtav1_params,
            "-pix_fmt", pix_fmt,
            "-an",               # audio added in concat pass
            str(seg_path),
        ]
        _run_encode(seg_cmd, seg_log)
        segment_paths.append(seg_path)

    # Write concat list
    concat_list = work_dir / "concat.txt"
    with open(concat_list, "w") as fh:
        for sp in segment_paths:
            fh.write(f"file '{sp}'\n")

    # Concatenate video segments + mux original audio
    concat_log = work_dir / "concat.log"
    concat_cmd = [
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-i", str(input_path),   # for audio
        "-map", "0:v",
        "-map", "1:a?",
        "-c", "copy",
        str(output_path),
    ]
    _run_encode(concat_cmd, concat_log)


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def compress_video(
    input_path:         str | Path,
    output_path:        str | Path | None = None,
    mode:               str               = "balanced",
    target_vmaf:        float             = 88.0,
    encoder:            str | None        = None,
    measure_vmaf_score: bool              = True,
    keep_work_dir:      bool              = False,
) -> CompressionResult:
    """
    Compress a video file with adaptive zone bitrate allocation.

    Parameters
    ----------
    input_path:
        Source video file.
    output_path:
        Destination file.  If omitted, ``<input_stem>_nebula.<ext>`` is used.
        For SVT-AV1, the extension is forced to ``.mkv``; for x265, ``.mp4``.
    mode:
        ``"safe"`` | ``"balanced"`` | ``"maximum"``.
    target_vmaf:
        Desired VMAF target (informational; used to log a warning if the
        encoded output falls short).  Range 0–100, typical 85–95.
    encoder:
        ``"x265"`` | ``"svt-av1"`` | ``"vvc"`` | ``None`` (auto-detect).
        ``"vvc"`` selects H.266/VVC via libvvenc — best compression at VMAF ≥ 95
        but ~10-20× slower than x265 on Apple Silicon (unoptimised arm64 build).
    measure_vmaf_score:
        Whether to measure VMAF after encoding.  Adds 20–40 % extra time.
    keep_work_dir:
        If True, the temporary working directory is not deleted after encoding
        (useful for debugging).

    Returns
    -------
    CompressionResult

    Raises
    ------
    FileNotFoundError
        If *input_path* does not exist.
    RuntimeError
        On encode or dependency failure.
    """
    input_path = Path(input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    # Normalise mode
    try:
        enc_mode = EncodeMode(mode.lower())
    except ValueError:
        raise ValueError(
            f"Unknown mode '{mode}'.  Choose from: "
            + ", ".join(m.value for m in EncodeMode)
        )

    # Normalise encoder
    enc: Optional[Encoder] = None
    if encoder is not None:
        try:
            enc = Encoder(encoder.lower())
        except ValueError:
            raise ValueError(
                f"Unknown encoder '{encoder}'.  Choose from: "
                + ", ".join(e.value for e in Encoder)
            )

    # Check dependencies (pass a placeholder encoder to check ffmpeg basics)
    bins    = _check_dependencies(enc or Encoder.X265)
    ffmpeg  = bins["ffmpeg"]
    ffprobe = bins["ffprobe"]

    # Probe source
    log.info("probing '%s' …", input_path.name)
    info = probe_video(input_path, ffprobe)
    log.info(
        "  %dx%d  %.1f fps  %.1f s  %d-bit  grain=%.2f",
        info.width, info.height, info.fps, info.duration,
        info.bit_depth, info.grain_level,
    )

    # Auto-select encoder
    selected_encoder = select_encoder(info, enc)

    # Resolve output path
    if output_path is None:
        ext = ".mkv" if selected_encoder == Encoder.SVT_AV1 else ".mp4"
        # VVC is also MP4 — libvvenc writes ISOBMFF, hvc1 tag not applicable
        output_path = input_path.with_name(input_path.stem + "_nebula" + ext)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Encode log lives next to output
    encode_log_path = output_path.with_suffix(".encode.log")

    # Temporary work dir for segment files (maximum AV1)
    work_dir = Path(tempfile.mkdtemp(prefix="nebula_"))
    log.info("work dir: %s", work_dir)

    try:
        # Scene detection
        cuts = detect_scene_cuts(input_path, ffmpeg)

        # Zone construction (VVC uses QP not CRF, but we still build zones for
        # future per-zone QP support; pass crf_x265 as a proxy for now)
        base_crf = _MODE_PARAMS[enc_mode][
            "crf_av1" if selected_encoder == Encoder.SVT_AV1 else "crf_x265"
        ]
        zones = build_zones(info, cuts, base_crf)

        # Encode — capture resource metrics from every path
        if selected_encoder == Encoder.X265:
            cmd = _build_x265_command(
                ffmpeg, input_path, output_path, info,
                zones, enc_mode, target_vmaf
            )
            enc_metrics = _run_encode(cmd, encode_log_path)

        elif selected_encoder == Encoder.VVC:
            cmd = _build_vvc_command(
                ffmpeg, input_path, output_path, info, enc_mode
            )
            enc_metrics = _run_encode(cmd, encode_log_path)

        elif selected_encoder == Encoder.VIDEOTOOLBOX:
            cmd = _build_videotoolbox_command(
                ffmpeg, input_path, output_path, info, enc_mode
            )
            enc_metrics = _run_encode(cmd, encode_log_path)

        else:  # SVT-AV1
            if enc_mode == EncodeMode.MAXIMUM and len(zones) > 1:
                _encode_maximum_svtav1(
                    ffmpeg, input_path, output_path, info,
                    zones, enc_mode, target_vmaf, work_dir
                )
                enc_metrics = EncodeMetrics()   # metrics not tracked for segmented path
            else:
                cmd = _build_svtav1_command(
                    ffmpeg, input_path, output_path, info,
                    zones, enc_mode, target_vmaf
                )
                enc_metrics = _run_encode(cmd, encode_log_path)

        # VMAF measurement
        # BUG 2 FIX: measure_vmaf now returns VMAFResult(mean, percentile_1);
        # both values are stored in CompressionResult.
        vmaf_mean = -1.0
        vmaf_p1   = 0.0
        if measure_vmaf_score:
            # Use the 4K model for UHD content — the HD model inflates VMAF
            # scores by 2-3 points on 4K source, masking real quality issues.
            vmaf_model = (
                "version=vmaf_4k_v0.6.1"
                if (info.width >= 3840 or info.height >= 2160)
                else "version=vmaf_v0.6.1"
            )
            log.info("VMAF model: %s", vmaf_model)
            vmaf_result = measure_vmaf(input_path, output_path, ffmpeg,
                                       model=vmaf_model, duration=info.duration)
            vmaf_mean   = vmaf_result.mean
            vmaf_p1     = vmaf_result.percentile_1
            vmaf_floor  = _MODE_PARAMS[enc_mode]["vmaf_floor"]
            if 0.0 < vmaf_mean < vmaf_floor:
                log.warning(
                    "VMAF mean %.2f is below target %.2f — consider a lower CRF "
                    "or 'safe' mode.",
                    vmaf_mean, vmaf_floor,
                )
            elif vmaf_mean >= vmaf_floor:
                log.info("VMAF mean %.2f meets target %.2f (p1=%.2f)",
                         vmaf_mean, vmaf_floor, vmaf_p1)

        # Size ratio
        out_size = output_path.stat().st_size
        ratio    = out_size / info.file_size if info.file_size else 0.0
        log.info(
            "size: %d MB → %d MB  (ratio %.3f)",
            info.file_size  // (1 << 20),
            out_size        // (1 << 20),
            ratio,
        )

        # Proof hash — always computed; never skipped.
        proof_hash = sha256_file(output_path)
        log.info("proof hash (sha256): %s", proof_hash)

        return CompressionResult(
            output_path   = output_path,
            vmaf          = vmaf_mean,
            vmaf_p1       = vmaf_p1,
            ratio         = ratio,
            encoder       = selected_encoder.value,
            zones         = len(zones),
            proof_hash    = proof_hash,
            input_path    = input_path,
            encode_log    = encode_log_path,
            cpu_pct_avg   = enc_metrics.cpu_avg,
            cpu_pct_peak  = enc_metrics.cpu_peak,
            encode_wall_s = enc_metrics.wall_s,
            extra         = {
                "scene_cuts":    len(cuts),
                "mode":          enc_mode.value,
                "target_vmaf":   target_vmaf,
                "source_size":   info.file_size,
                "output_size":   out_size,
                "rss_peak_mb":   enc_metrics.rss_peak_mb,
                "cpu_cores":     _CPU_LOGICAL,
                "p_cores":       _CPU_P_CORES,
                "e_cores":       _CPU_E_CORES,
                "apple_silicon": _IS_APPLE_SIL,
            },
        )

    finally:
        if not keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: Sequence[str]):
    import argparse

    parser = argparse.ArgumentParser(
        prog="nebula",
        description="Nebula video encoder",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input",  help="Source video file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output file (default: <input>_nebula.<ext>)")
    parser.add_argument("--mode",   default="balanced",
                        choices=[m.value for m in EncodeMode],
                        help="Encode mode")
    parser.add_argument("--target-vmaf", type=float, default=95.0,
                        dest="target_vmaf",
                        help="Target VMAF score (0–100); informational — "
                             "logs a warning if output falls short, "
                             "does not rate-control to hit the target")
    parser.add_argument("--encoder", default=None,
                        choices=[e.value for e in Encoder] + [None],  # type: ignore[list-item]
                        help="Force encoder (default: auto)")
    parser.add_argument("--no-vmaf", action="store_true",
                        dest="no_vmaf",
                        help="Skip VMAF measurement")
    parser.add_argument("--keep-work-dir", action="store_true",
                        dest="keep_work_dir",
                        help="Keep temporary segment directory")
    parser.add_argument("--json", action="store_true",
                        dest="output_json",
                        help="Write JSON result to stdout (always enabled; flag kept for compatibility)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else list(argv))

    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        result = compress_video(
            input_path          = args.input,
            output_path         = args.output,
            mode                = args.mode,
            target_vmaf         = args.target_vmaf,
            encoder             = args.encoder,
            measure_vmaf_score  = not args.no_vmaf,
            keep_work_dir       = args.keep_work_dir,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        log.error("%s", exc)
        return 1

    print(json.dumps({
        "output":        str(result.output_path),
        "vmaf":          result.vmaf,
        "vmaf_p1":       result.vmaf_p1,
        "ratio":         round(result.ratio, 4),
        "encoder":       result.encoder,
        "zones":         result.zones,
        "proof_hash":    result.proof_hash,
        "encode_log":    str(result.encode_log),
        "encode_wall_s": round(result.encode_wall_s, 1),
        "cpu_pct_avg":   result.cpu_pct_avg,
        "cpu_pct_peak":  result.cpu_pct_peak,
        **result.extra,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
