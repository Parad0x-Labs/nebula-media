# MIT License
#
# Copyright (c) 2026 Parad0x Labs
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Pure numpy/scipy video quality metrics: SSIM, PSNR, MS-SSIM.

All metric functions operate on (H, W) float64 luma planes unless noted.
Frame I/O uses raw FFmpeg pipes delivering rgb24 frames.
"""

from __future__ import annotations

import subprocess
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter


# ---------------------------------------------------------------------------
# Luma conversion
# ---------------------------------------------------------------------------

def rgb_to_y(frame_rgb: np.ndarray) -> np.ndarray:
    """Convert (H, W, 3) uint8 RGB to (H, W) float64 luma using BT.709 coefficients."""
    r = frame_rgb[:, :, 0].astype(np.float64)
    g = frame_rgb[:, :, 1].astype(np.float64)
    b = frame_rgb[:, :, 2].astype(np.float64)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------------------
# SSIM
# ---------------------------------------------------------------------------

# Constants matching scikit-image defaults for 8-bit data.
_C1 = (0.01 * 255) ** 2   # 6.5025
_C2 = (0.03 * 255) ** 2   # 58.5225
_SSIM_SIGMA = 1.5
_SSIM_TRUNCATE = 3.5       # scipy gaussian_filter default


def _ssim_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Compute the per-pixel SSIM map for two float64 luma planes.

    Follows scikit-image structural_similarity with gaussian_weights=True,
    sigma=1.5, truncate=3.5 (11x11 effective window).
    """
    mu1 = gaussian_filter(a, sigma=_SSIM_SIGMA, truncate=_SSIM_TRUNCATE)
    mu2 = gaussian_filter(b, sigma=_SSIM_SIGMA, truncate=_SSIM_TRUNCATE)

    mu1_sq = mu1 * mu1
    mu2_sq = mu2 * mu2
    mu12 = mu1 * mu2

    # Compute variances/covariance: E[X^2] - (E[X])^2
    # The squared frame is filtered, NOT the filtered frame squared.
    sigma1_sq = gaussian_filter(a * a, sigma=_SSIM_SIGMA, truncate=_SSIM_TRUNCATE) - mu1_sq
    sigma2_sq = gaussian_filter(b * b, sigma=_SSIM_SIGMA, truncate=_SSIM_TRUNCATE) - mu2_sq
    sigma12   = gaussian_filter(a * b, sigma=_SSIM_SIGMA, truncate=_SSIM_TRUNCATE) - mu12

    # Clamp variances to zero; covariance is allowed to be negative.
    np.clip(sigma1_sq, 0, None, out=sigma1_sq)
    np.clip(sigma2_sq, 0, None, out=sigma2_sq)

    numerator   = (2.0 * mu12 + _C1) * (2.0 * sigma12 + _C2)
    denominator = (mu1_sq + mu2_sq + _C1) * (sigma1_sq + sigma2_sq + _C2)

    return numerator / denominator


def compute_ssim(ref: np.ndarray, dis: np.ndarray, crop_border: int = 5) -> float:
    """
    Compute mean SSIM between two (H, W) float64 luma frames.

    crop_border: pixels to trim from each edge of the SSIM map before averaging.
    Returns a float in (-1, 1]; 1.0 for identical inputs.
    """
    smap = _ssim_map(ref, dis)
    if crop_border > 0:
        smap = smap[crop_border:-crop_border, crop_border:-crop_border]
    return float(smap.mean())


# ---------------------------------------------------------------------------
# PSNR
# ---------------------------------------------------------------------------

def compute_psnr(ref: np.ndarray, dis: np.ndarray, max_val: float = 255.0) -> float:
    """
    Peak signal-to-noise ratio between two (H, W) float64 luma frames.

    Returns float('inf') when the frames are identical (MSE == 0).
    """
    diff = ref.astype(np.float64) - dis.astype(np.float64)
    mse = float(np.mean(diff * diff))
    if mse == 0.0:
        return float("inf")
    return 10.0 * np.log10((max_val ** 2) / mse)


# ---------------------------------------------------------------------------
# MS-SSIM
# ---------------------------------------------------------------------------

# Wang 2003 weights for 5 scales.
_MS_SSIM_WEIGHTS = np.array([0.0448, 0.2856, 0.3001, 0.2363, 0.1333], dtype=np.float64)


def _box_downsample(img: np.ndarray) -> np.ndarray:
    """2x2 box-filter downsample. Slices to even dimensions first."""
    h, w = img.shape
    h2 = h - (h % 2)
    w2 = w - (w % 2)
    img = img[:h2, :w2]
    return (img[::2, ::2] + img[1::2, ::2] + img[::2, 1::2] + img[1::2, 1::2]) / 4.0


def compute_ms_ssim(ref: np.ndarray, dis: np.ndarray, n_scales: int = 5) -> float:
    """
    Multi-scale SSIM (Wang 2003) on two (H, W) float64 luma frames.

    Uses 5-scale weights [0.0448, 0.2856, 0.3001, 0.2363, 0.1333].
    Stops early if the minimum spatial dimension drops below 11 pixels;
    the remaining weights are renormalized so they still sum to 1.
    """
    weights = _MS_SSIM_WEIGHTS[:n_scales].copy()

    a = ref.astype(np.float64)
    b = dis.astype(np.float64)

    ssim_per_scale: List[float] = []
    used_weights: List[float] = []

    for k in range(n_scales):
        if min(a.shape) < 11:
            # Cannot compute a meaningful SSIM at this resolution; stop.
            break

        s = compute_ssim(a, b, crop_border=0)
        ssim_per_scale.append(s)
        used_weights.append(weights[k])

        if k < n_scales - 1:
            a = _box_downsample(a)
            b = _box_downsample(b)

    if not ssim_per_scale:
        return float("nan")

    w = np.array(used_weights, dtype=np.float64)
    w /= w.sum()  # renormalize in case we stopped early

    # MS-SSIM = product of SSIM^w_k across scales.
    # Clamp SSIM values to a small positive floor before exponentiation
    # to avoid domain errors on pathological inputs.
    result = 1.0
    for s, wi in zip(ssim_per_scale, w):
        result *= max(s, 1e-12) ** wi

    return float(result)


# ---------------------------------------------------------------------------
# Frame-pair iterator via FFmpeg pipes
# ---------------------------------------------------------------------------

def _probe_video_size(path: Path, ffmpeg: str) -> Tuple[int, int]:
    """Return (width, height) for the first video stream via ffprobe."""
    ffprobe = str(Path(ffmpeg).parent / "ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    w, h = out.split(",")
    return int(w), int(h)


def iter_frame_pairs_rgb(
    ref_path: Path,
    dis_path: Path,
    ffmpeg: str,
    n_subsample: int = 1,
) -> Iterator[Tuple[int, np.ndarray, np.ndarray]]:
    """
    Yield (frame_idx, ref_rgb, dis_rgb) pairs from two video files.

    Both frames are (H, W, 3) uint8 in RGB order.
    n_subsample=N means only every Nth frame is yielded (0-indexed:
    frame 0, N, 2N, …). Both pipes are always read to avoid blocking.
    """
    ref_path = Path(ref_path)
    dis_path = Path(dis_path)

    w, h = _probe_video_size(ref_path, ffmpeg)
    frame_size = h * w * 3  # bytes per rgb24 frame

    def _open_pipe(path: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [
                ffmpeg,
                "-loglevel", "error",
                "-i", str(path),
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    ref_proc = _open_pipe(ref_path)
    dis_proc = _open_pipe(dis_path)

    try:
        frame_idx = 0
        while True:
            ref_raw = ref_proc.stdout.read(frame_size)
            dis_raw = dis_proc.stdout.read(frame_size)

            if len(ref_raw) < frame_size or len(dis_raw) < frame_size:
                break

            if frame_idx % n_subsample == 0:
                ref_rgb = np.frombuffer(ref_raw, dtype=np.uint8).reshape(h, w, 3).copy()
                dis_rgb = np.frombuffer(dis_raw, dtype=np.uint8).reshape(h, w, 3).copy()
                yield frame_idx, ref_rgb, dis_rgb

            frame_idx += 1
    finally:
        ref_proc.stdout.close()
        dis_proc.stdout.close()
        ref_proc.wait()
        dis_proc.wait()


# ---------------------------------------------------------------------------
# Per-frame and clip dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FrameQuality:
    frame_index: int
    ssim: float
    psnr: float


@dataclass
class ClipQualityMetrics:
    ssim_mean: float
    ssim_p1: float      # 1st percentile — worst frames
    ssim_min: float
    psnr_mean: float
    psnr_min: float
    frame_count: int
    sampled_count: int
    frame_metrics: List[FrameQuality] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Clip-level measurement
# ---------------------------------------------------------------------------

def measure_clip_quality(
    ref_path: Path,
    dis_path: Path,
    ffmpeg: str,
    n_subsample: int = 6,
) -> ClipQualityMetrics:
    """
    Measure SSIM and PSNR across a clip by sampling every n_subsample-th frame.

    Returns a ClipQualityMetrics with aggregate statistics and per-frame detail.
    """
    ref_path = Path(ref_path)
    dis_path = Path(dis_path)

    frame_metrics: List[FrameQuality] = []
    total_frames = 0

    for frame_idx, ref_rgb, dis_rgb in iter_frame_pairs_rgb(
        ref_path, dis_path, ffmpeg, n_subsample=n_subsample
    ):
        # Track total frames seen (iterator only yields sampled ones, so
        # reconstruct a lower bound from sampled indices).
        ref_y = rgb_to_y(ref_rgb)
        dis_y = rgb_to_y(dis_rgb)

        ssim = compute_ssim(ref_y, dis_y)
        psnr = compute_psnr(ref_y, dis_y)
        frame_metrics.append(FrameQuality(frame_index=frame_idx, ssim=ssim, psnr=psnr))

    sampled_count = len(frame_metrics)

    if sampled_count == 0:
        return ClipQualityMetrics(
            ssim_mean=float("nan"),
            ssim_p1=float("nan"),
            ssim_min=float("nan"),
            psnr_mean=float("nan"),
            psnr_min=float("nan"),
            frame_count=0,
            sampled_count=0,
            frame_metrics=[],
        )

    ssim_arr = np.array([f.ssim for f in frame_metrics], dtype=np.float64)
    psnr_arr = np.array([f.psnr for f in frame_metrics], dtype=np.float64)

    # For frame_count we use the last sampled frame index + 1 as a lower bound;
    # callers that need exact total-frame count should probe separately.
    last_idx = frame_metrics[-1].frame_index
    estimated_total = last_idx + 1  # conservative floor

    return ClipQualityMetrics(
        ssim_mean=float(np.mean(ssim_arr)),
        ssim_p1=float(np.percentile(ssim_arr, 1)),
        ssim_min=float(np.min(ssim_arr)),
        psnr_mean=float(np.nanmean(psnr_arr)),
        psnr_min=float(np.nanmin(psnr_arr)),
        frame_count=estimated_total,
        sampled_count=sampled_count,
        frame_metrics=frame_metrics,
    )


# ---------------------------------------------------------------------------
# Sanity check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import math

    rng = np.random.default_rng(42)

    # --- identical frames: SSIM must be 1.0, PSNR must be inf ---
    a = rng.integers(0, 256, size=(128, 128), dtype=np.uint8).astype(np.float64)
    ssim_identical = compute_ssim(a, a)
    psnr_identical = compute_psnr(a, a)

    assert ssim_identical == 1.0, f"SSIM of identical frames: {ssim_identical} != 1.0"
    assert math.isinf(psnr_identical), f"PSNR of identical frames: {psnr_identical} != inf"
    print(f"Identical:  SSIM={ssim_identical:.6f}  PSNR={psnr_identical}")

    # --- frames differing by a constant 10: SSIM should be high ---
    b = np.clip(a + 10.0, 0, 255)
    ssim_diff = compute_ssim(a, b)
    psnr_diff = compute_psnr(a, b)

    assert 0.8 <= ssim_diff <= 1.0, f"SSIM of +10 frames: {ssim_diff} not in [0.8, 1.0]"
    print(f"Offset +10: SSIM={ssim_diff:.6f}  PSNR={psnr_diff:.2f} dB")

    # --- MS-SSIM sanity ---
    ms_ssim_identical = compute_ms_ssim(a, a)
    assert abs(ms_ssim_identical - 1.0) < 1e-9, f"MS-SSIM identical: {ms_ssim_identical}"
    ms_ssim_diff = compute_ms_ssim(a, b)
    assert 0.8 <= ms_ssim_diff <= 1.0, f"MS-SSIM +10: {ms_ssim_diff}"
    print(f"MS-SSIM identical={ms_ssim_identical:.8f}  offset={ms_ssim_diff:.6f}")

    print("All assertions passed.")
