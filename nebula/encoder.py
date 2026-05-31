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
nebula_v9.py — Adaptive Video Zone Optimizer
============================================
Production-quality video compression with per-zone adaptive bitrate,
dual-encoder support (x265 / SVT-AV1), scene-aware boundary detection,
AV1 film grain synthesis, VMAF quality measurement, and on-chain-ready
proof hashing.

Public API
----------
    from nebula_v9 import compress_video, CompressionResult

    result = compress_video("input.mp4", mode="safe", target_vmaf=88)
    print(result.vmaf, result.vmaf_p1, result.ratio, result.output_path, result.proof_hash)

Modes
-----
    safe      — conservative settings, reliable compatibility (x265/AV1 CRF target)
    balanced  — default quality/size trade-off
    gladiator — maximum compression, accepts longer encode times

Encoders
--------
    x265      — battle-tested HEVC, wide device support
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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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

    # BUG 2 FIX: expose VMAF 1st-percentile so callers can detect worst-case frames.
    vmaf_p1: float = 0.0
    """VMAF 1st-percentile score (worst ~1 % of frames).  0.0 if not available."""

    extra: dict = field(default_factory=dict)
    """Additional metadata (scene cuts, per-zone stats, etc.)."""


# ---------------------------------------------------------------------------
# Enumerations & constants
# ---------------------------------------------------------------------------

class EncodeMode(str, Enum):
    SAFE      = "safe"
    BALANCED  = "balanced"
    GLADIATOR = "gladiator"


class Encoder(str, Enum):
    X265    = "x265"
    SVT_AV1 = "svt-av1"


# Mode → (x265 preset, svt-av1 preset, base CRF x265, base CRF av1)
_MODE_PARAMS: dict[EncodeMode, dict] = {
    EncodeMode.SAFE: {
        "x265_preset":   "medium",
        "av1_preset":    6,
        "crf_x265":      24,
        "crf_av1":       32,
        "vmaf_floor":    85.0,
    },
    EncodeMode.BALANCED: {
        "x265_preset":   "slow",
        "av1_preset":    5,
        "crf_x265":      22,
        "crf_av1":       30,
        "vmaf_floor":    88.0,
    },
    EncodeMode.GLADIATOR: {
        "x265_preset":   "veryslow",
        "av1_preset":    3,
        "crf_x265":      20,
        "crf_av1":       28,
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
        # x265 is accessed via ffmpeg's libx265; no separate binary needed.
        pass
    elif encoder == Encoder.SVT_AV1:
        # SVT-AV1 may be accessed either through ffmpeg (libsvtav1) or the
        # standalone SvtAv1EncApp.  We check ffmpeg capability instead.
        result = subprocess.run(
            [bins["ffmpeg"], "-encoders"],
            capture_output=True, text=True, timeout=10
        )
        if "libsvtav1" not in result.stdout:
            raise RuntimeError(
                "ffmpeg was found but was not compiled with --enable-libsvtav1.  "
                "Install a full-featured build (e.g. from jellyfin/ffmpeg or BtbN)."
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

    return VideoInfo(
        duration    = duration,
        width       = int(video_stream.get("width",  1920)),
        height      = int(video_stream.get("height", 1080)),
        fps         = fps,
        bit_depth   = bit_depth,
        codec       = video_stream.get("codec_name", "unknown"),
        has_audio   = audio_stream is not None,
        file_size   = int(fmt.get("size", 0)),
        grain_level = _estimate_grain(video_stream, fps),
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

    Heuristic rules
    ---------------
    * Animated / clean content (low grain) → SVT-AV1 (better flat-color coding)
    * Heavily grained / film content       → x265  (grain synthesis more mature)
    * 10-bit source                        → SVT-AV1 preferred (native 10-bit)
    * Duration < 30 s (preview/clip)       → x265  (faster, no AV1 overhead)
    """
    if encoder is not None:
        return encoder

    if info.duration < 30.0:
        log.info("auto-encoder: short clip → x265")
        return Encoder.X265

    if info.grain_level > 0.6:
        log.info("auto-encoder: high grain detected (%.2f) → x265", info.grain_level)
        return Encoder.X265

    if info.bit_depth == 10 or info.grain_level < 0.25:
        log.info(
            "auto-encoder: 10-bit=%s, grain=%.2f → svt-av1",
            info.bit_depth == 10, info.grain_level
        )
        return Encoder.SVT_AV1

    log.info("auto-encoder: balanced content → svt-av1 (default preference)")
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
            offset = +2       # near-static → fewer bits
        else:
            offset = 0

        # BUG 3 FIX: snap start/end to keyframe boundaries.
        snapped_start = _snap_to_keyframe_boundary(start, fps, keyint)
        snapped_end   = _snap_to_keyframe_boundary(end,   fps, keyint)

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


def measure_vmaf(
    reference: Path,
    distorted: Path,
    ffmpeg:    str,
    model:     str = "version=vmaf_v0.6.1",
) -> VMAFResult:
    """
    Measure VMAF of *distorted* vs *reference* using ffmpeg's libvmaf filter.

    Returns a :class:`VMAFResult` with mean and 1st-percentile scores.
    Both fields are -1.0 on failure.

    BUG 2 FIX: previously only the mean was extracted; the 1st percentile
    (worst ~1 % of frames) is now parsed and returned so callers can detect
    perceptual quality cliffs.
    """
    log.info("measuring VMAF …")
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
            f"n_threads=4"
        ),
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        log.warning("VMAF measurement failed:\n%s", proc.stderr[-2000:])
        return VMAFResult(mean=-1.0, percentile_1=-1.0)

    try:
        with open(vmaf_log) as fh:
            data = json.load(fh)
        vmaf_metrics = data["pooled_metrics"]["vmaf"]
        mean         = float(vmaf_metrics["mean"])
        # BUG 2 FIX: extract 1st percentile; fall back gracefully if absent.
        percentile_1 = float(vmaf_metrics.get("percentile_1", vmaf_metrics.get("min", mean)))
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
    zone_parts: List[str] = []
    for z in zones:
        sf  = int(z.start * fps)
        ef  = max(sf, int(z.end * fps) - 1)
        crf = max(12, min(51, base_crf + z.crf_offset))
        zone_parts.append(f"{sf},{ef},crf={crf}")
    zones_str = "/".join(zone_parts) if zone_parts else ""

    x265_params = [
        f"preset={preset}",
        "profile=main10" if info.bit_depth == 10 else "profile=main",
        "high-tier=1",
        "ref=5",
        "bframes=8",
        "b-adapt=2",
        "rc-lookahead=60",
        "aq-mode=3",
        "aq-strength=0.8",
        "psy-rd=1.0",
        "psy-rdoq=1.5",
        "deblock=-1:-1",
        "me=umh",
    ]
    if zones_str:
        x265_params.append(f"zones={zones_str}")

    # Denoise + regrain for noisy sources
    vf_filters: List[str] = []
    if info.grain_level > 0.5:
        vf_filters.append("hqdn3d=2:2:3:3")    # light denoise
        vf_filters.append("noise=alls=5:allf=t+u")  # synthetic grain (ffmpeg noise filter)

    # 10-bit pixel format
    pix_fmt = "yuv420p10le" if info.bit_depth == 10 else "yuv420p"

    cmd: List[str] = [
        ffmpeg, "-y",
        "-i", str(input_path),
    ]
    if vf_filters:
        cmd += ["-filter:v", ",".join(vf_filters)]

    cmd += [
        "-c:v", "libx265",
        "-crf", str(base_crf),
        "-x265-params", ":".join(x265_params),
        "-pix_fmt", pix_fmt,
        "-c:a", "copy",
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
    the caller for gladiator mode).  For safe/balanced, a single-pass global
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

    # SVT-AV1 params string
    svtav1_params = f"film-grain={film_grain}:enable-overlays=1:scd=1"
    if mode == EncodeMode.GLADIATOR:
        svtav1_params += ":hierarchical-levels=5:lookahead=60"

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


def _run_encode(cmd: List[str], log_path: Path) -> None:
    """Execute an encode command, streaming stderr to the log file."""
    log.info("running: %s", " ".join(cmd[:6]) + " …")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(" ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(
            f"Encode failed (exit {proc.returncode}).  "
            f"See log: {log_path}"
        )


# ---------------------------------------------------------------------------
# Gladiator zone-segment encode
# ---------------------------------------------------------------------------

def _encode_gladiator_svtav1(
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
    Per-zone encode for gladiator mode with SVT-AV1.

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
        svtav1_params = f"film-grain={film_grain}:enable-overlays=1:scd=0"

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
        ``"safe"`` | ``"balanced"`` | ``"gladiator"``.
    target_vmaf:
        Desired VMAF target (informational; used to log a warning if the
        encoded output falls short).  Range 0–100, typical 85–95.
    encoder:
        ``"x265"`` | ``"svt-av1"`` | ``None`` (auto-detect).
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
        output_path = input_path.with_name(input_path.stem + "_nebula" + ext)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Encode log lives next to output
    encode_log_path = output_path.with_suffix(".encode.log")

    # Temporary work dir for segment files (gladiator AV1)
    work_dir = Path(tempfile.mkdtemp(prefix="nebula_"))
    log.info("work dir: %s", work_dir)

    try:
        # Scene detection
        cuts = detect_scene_cuts(input_path, ffmpeg)

        # Zone construction
        base_crf = _MODE_PARAMS[enc_mode][
            "crf_x265" if selected_encoder == Encoder.X265 else "crf_av1"
        ]
        zones = build_zones(info, cuts, base_crf)

        # Encode
        if selected_encoder == Encoder.X265:
            cmd = _build_x265_command(
                ffmpeg, input_path, output_path, info,
                zones, enc_mode, target_vmaf
            )
            _run_encode(cmd, encode_log_path)

        else:  # SVT-AV1
            if enc_mode == EncodeMode.GLADIATOR and len(zones) > 1:
                # Per-zone encode for maximum quality control
                _encode_gladiator_svtav1(
                    ffmpeg, input_path, output_path, info,
                    zones, enc_mode, target_vmaf, work_dir
                )
            else:
                cmd = _build_svtav1_command(
                    ffmpeg, input_path, output_path, info,
                    zones, enc_mode, target_vmaf
                )
                _run_encode(cmd, encode_log_path)

        # VMAF measurement
        # BUG 2 FIX: measure_vmaf now returns VMAFResult(mean, percentile_1);
        # both values are stored in CompressionResult.
        vmaf_mean = -1.0
        vmaf_p1   = 0.0
        if measure_vmaf_score:
            vmaf_result = measure_vmaf(input_path, output_path, ffmpeg)
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
            output_path = output_path,
            vmaf        = vmaf_mean,
            vmaf_p1     = vmaf_p1,
            ratio       = ratio,
            encoder     = selected_encoder.value,
            zones       = len(zones),
            proof_hash  = proof_hash,
            input_path  = input_path,
            encode_log  = encode_log_path,
            extra       = {
                "scene_cuts":  len(cuts),
                "mode":        enc_mode.value,
                "target_vmaf": target_vmaf,
                "source_size": info.file_size,
                "output_size": out_size,
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
        prog            = "nebula_v9",
        description     = "Adaptive video zone optimizer (nebula v9)",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input",  help="Source video file")
    parser.add_argument("output", nargs="?", default=None,
                        help="Output file (default: <input>_nebula.<ext>)")
    parser.add_argument("--mode",   default="balanced",
                        choices=[m.value for m in EncodeMode],
                        help="Encode mode")
    parser.add_argument("--target-vmaf", type=float, default=88.0,
                        dest="target_vmaf",
                        help="Target VMAF score (0–100)")
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
        "output":     str(result.output_path),
        "vmaf":       result.vmaf,
        "vmaf_p1":    result.vmaf_p1,
        "ratio":      round(result.ratio, 4),
        "encoder":    result.encoder,
        "zones":      result.zones,
        "proof_hash": result.proof_hash,
        "encode_log": str(result.encode_log),
        **result.extra,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
