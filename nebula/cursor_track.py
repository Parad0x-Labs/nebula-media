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
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import annotations

import hashlib
import io
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np
from scipy.ndimage import uniform_filter
from scipy.signal import correlate2d


class CursorShape(IntEnum):
    ARROW = 0
    IBEAM = 1
    CROSSHAIR = 2
    HAND = 3
    RESIZE_NS = 4
    RESIZE_EW = 5
    RESIZE_NWSE = 6
    RESIZE_NESW = 7
    WAIT = 8
    NOT_ALLOWED = 9
    DRAG = 10
    UNKNOWN = 255


@dataclass
class CursorFrame:
    frame_index: int
    x: int           # hotspot X, frame pixel coords
    y: int           # hotspot Y, frame pixel coords
    shape: CursorShape
    confidence: float  # NCC score 0.0-1.0
    hidden: bool


@dataclass
class CursorTrack:
    fps: float
    width: int
    height: int
    frames: List[CursorFrame]

    MAGIC = b'NCTK'
    VERSION = 1
    ANCHOR_MARKER = 0xFF
    HIDDEN_MARKER = 0xFE
    ANCHOR_INTERVAL = 30

    def encode(self) -> bytes:
        """
        Binary format:
          Header: MAGIC(4) + VERSION(1) + fps_int16(2) + width_uint16(2) + height_uint16(2) + frame_count_uint32(4) = 15 bytes
          Per frame: either:
            ANCHOR: 0xFF + x_uint16(2) + y_uint16(2) + shape_uint8(1) = 6 bytes
            DELTA:  i8_dx(1) + i8_dy(1) = 2 bytes  (when |dx|<=127 and |dy|<=127 and not hidden)
            HIDDEN: 0xFE = 1 byte
          Force ANCHOR on: first frame, every ANCHOR_INTERVAL frames, teleport (|delta|>127), shape change, hidden->visible
        """
        buf = io.BytesIO()

        fps_int = int(round(self.fps))
        buf.write(self.MAGIC)
        buf.write(struct.pack('B', self.VERSION))
        buf.write(struct.pack('>hHHI',
            fps_int,
            self.width,
            self.height,
            len(self.frames),
        ))

        prev_x = 0
        prev_y = 0
        prev_shape = CursorShape.ARROW
        prev_hidden = False

        for i, fr in enumerate(self.frames):
            force_anchor = (
                i == 0
                or (i % self.ANCHOR_INTERVAL) == 0
            )

            if fr.hidden:
                buf.write(struct.pack('B', self.HIDDEN_MARKER))
                prev_hidden = True
                continue

            dx = fr.x - prev_x
            dy = fr.y - prev_y
            shape_changed = (fr.shape != prev_shape)
            teleport = abs(dx) > 127 or abs(dy) > 127
            back_from_hidden = prev_hidden

            if force_anchor or teleport or shape_changed or back_from_hidden:
                buf.write(struct.pack('>BHHB',
                    self.ANCHOR_MARKER,
                    fr.x,
                    fr.y,
                    int(fr.shape),
                ))
            else:
                buf.write(struct.pack('bb', dx, dy))

            prev_x = fr.x
            prev_y = fr.y
            prev_shape = fr.shape
            prev_hidden = False

        return buf.getvalue()

    @classmethod
    def decode(cls, data: bytes) -> 'CursorTrack':
        buf = io.BytesIO(data)

        magic = buf.read(4)
        if magic != cls.MAGIC:
            raise ValueError(f"Bad magic: {magic!r}")

        version = struct.unpack('B', buf.read(1))[0]
        if version != cls.VERSION:
            raise ValueError(f"Unknown version: {version}")

        fps_int, width, height, frame_count = struct.unpack('>hHHI', buf.read(10))
        fps = float(fps_int)

        frames: List[CursorFrame] = []
        cur_x = 0
        cur_y = 0
        cur_shape = CursorShape.ARROW
        prev_hidden = False

        for i in range(frame_count):
            b = buf.read(1)
            if not b:
                raise ValueError(f"Unexpected end of data at frame {i}")
            marker = b[0]

            if marker == cls.HIDDEN_MARKER:
                frames.append(CursorFrame(
                    frame_index=i,
                    x=cur_x,
                    y=cur_y,
                    shape=cur_shape,
                    confidence=0.0,
                    hidden=True,
                ))
                prev_hidden = True
                continue

            if marker == cls.ANCHOR_MARKER:
                x, y, shape_byte = struct.unpack('>HHB', buf.read(5))
                cur_x = x
                cur_y = y
                try:
                    cur_shape = CursorShape(shape_byte)
                except ValueError:
                    cur_shape = CursorShape.UNKNOWN
                prev_hidden = False
            else:
                # marker is the first byte of a signed delta pair
                dx = struct.unpack('b', bytes([marker]))[0]
                dy = struct.unpack('b', buf.read(1))[0]
                cur_x += dx
                cur_y += dy
                prev_hidden = False

            frames.append(CursorFrame(
                frame_index=i,
                x=cur_x,
                y=cur_y,
                shape=cur_shape,
                confidence=1.0,
                hidden=False,
            ))

        return cls(fps=fps, width=width, height=height, frames=frames)

    def to_json_dict(self) -> dict:
        return {
            'fps': self.fps,
            'width': self.width,
            'height': self.height,
            'frames': [
                {
                    'frame_index': fr.frame_index,
                    'x': fr.x,
                    'y': fr.y,
                    'shape': int(fr.shape),
                    'confidence': fr.confidence,
                    'hidden': fr.hidden,
                }
                for fr in self.frames
            ],
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> 'CursorTrack':
        frames = [
            CursorFrame(
                frame_index=f['frame_index'],
                x=f['x'],
                y=f['y'],
                shape=CursorShape(f['shape']),
                confidence=f['confidence'],
                hidden=f['hidden'],
            )
            for f in d['frames']
        ]
        return cls(
            fps=d['fps'],
            width=d['width'],
            height=d['height'],
            frames=frames,
        )


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

def _make_arrow_template() -> np.ndarray:
    """
    Return a 32x32 uint8 grayscale synthetic macOS arrow cursor template.
    Draw the arrow programmatically using numpy — no image file dependency.
    The arrow points up-left from top-left corner (standard macOS arrow).
    """
    tmpl = np.zeros((32, 32), dtype=np.uint8)
    # Fill the arrow shape with 200 (light), anti-alias by setting diagonal to 128
    for row in range(24):
        tmpl[row, :max(1, 24 - row)] = 200
    # Outline in white
    tmpl[0, :24] = 255
    tmpl[:24, 0] = 255
    return tmpl


# ---------------------------------------------------------------------------
# NCC helper
# ---------------------------------------------------------------------------

def _ncc_map(patch: np.ndarray, tmpl: np.ndarray) -> float:
    """
    Compute scalar NCC between patch and tmpl (same shape).
    Returns value in [-1, 1]; higher = better match.
    """
    p = patch.astype(np.float32)
    t = tmpl.astype(np.float32)
    p -= p.mean()
    t -= t.mean()
    denom = np.sqrt((p ** 2).sum() * (t ** 2).sum())
    if denom < 1e-6:
        return 0.0
    return float(np.sum(p * t) / denom)


def _sliding_ncc(crop: np.ndarray, tmpl: np.ndarray) -> Tuple[float, int, int]:
    """
    Compute sliding NCC of tmpl over crop using scipy.signal.correlate2d.
    Returns (best_score, best_row, best_col) in crop coords (top-left of template).
    """
    th, tw = tmpl.shape
    ch, cw = crop.shape

    if ch < th or cw < tw:
        score = _ncc_map(crop, tmpl[:ch, :tw])
        return score, 0, 0

    crop_f = crop.astype(np.float32)
    tmpl_f = tmpl.astype(np.float32)

    # Subtract local mean from template
    tmpl_centered = tmpl_f - tmpl_f.mean()
    tmpl_var = float((tmpl_centered ** 2).sum())

    # Cross-correlation via scipy: mode='valid' gives (ch-th+1, cw-tw+1)
    cross = correlate2d(crop_f, tmpl_centered[::-1, ::-1], mode='valid')

    # Local patch mean via uniform_filter on padded crop
    pad_h = th // 2
    pad_w = tw // 2
    crop_padded = np.pad(crop_f, ((pad_h, pad_h), (pad_w, pad_w)), mode='reflect')
    local_mean = uniform_filter(crop_padded, size=(th, tw))[pad_h:pad_h + ch, pad_w:pad_w + cw]
    local_mean_valid = local_mean[:ch - th + 1, :cw - tw + 1]

    N = th * tw
    # Local patch energy: sum of (patch - local_mean)^2
    crop_sq = crop_f ** 2
    crop_sq_padded = np.pad(crop_sq, ((pad_h, pad_h), (pad_w, pad_w)), mode='reflect')
    local_sq_mean = uniform_filter(crop_sq_padded, size=(th, tw))[pad_h:pad_h + ch, pad_w:pad_w + cw]
    local_sq_mean_valid = local_sq_mean[:ch - th + 1, :cw - tw + 1]

    patch_var = (local_sq_mean_valid - local_mean_valid ** 2) * N
    patch_var = np.maximum(patch_var, 0.0)

    denom = np.sqrt(patch_var * tmpl_var)
    ncc = np.where(denom > 1e-6, cross / denom, 0.0)

    idx = np.argmax(ncc)
    rows, cols = divmod(idx, ncc.shape[1])
    best_score = float(ncc.flat[idx])
    return best_score, int(rows), int(cols)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_cursor_track(
    video_path: Path,
    ffmpeg: str,
    width: int,
    height: int,
    fps: float,
    search_window: int = 200,
    ncc_threshold: float = 0.75,
    diff_threshold: int = 30,
    anchor_interval: int = 30,
) -> CursorTrack:
    """
    Two-stage cursor detection:

    Stage 1 — Frame differencing to find motion candidates:
      diff = |frame_n_gray - frame_{n-1}_gray|
      candidate = diff > diff_threshold
      centroid of candidate region = possible cursor position

    Stage 2 — Template NCC in a search_window around the candidate:
      Crop search_window x search_window region around candidate centroid.
      Compute NCC between crop and arrow template (resize template to fit).
      If NCC > ncc_threshold: cursor confirmed at arg-max of NCC map.
      Else: cursor hidden.

    Returns CursorTrack with one CursorFrame per video frame.
    Frame 0: candidate from single-frame heuristic (brightest motion region).
    """
    tmpl = _make_arrow_template()
    tmpl_h, tmpl_w = tmpl.shape

    frame_bytes = width * height
    cmd = [
        ffmpeg, '-i', str(video_path),
        '-f', 'rawvideo',
        '-pix_fmt', 'gray',
        '-vf', f'scale={width}:{height}',
        'pipe:1',
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    frames: List[CursorFrame] = []
    prev_gray: Optional[np.ndarray] = None
    frame_index = 0
    cur_x = width // 2
    cur_y = height // 2
    half_win = search_window // 2

    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break

            gray = np.frombuffer(raw, dtype=np.uint8).reshape((height, width))

            if frame_index == 0:
                # Heuristic: brightest region via uniform_filter as initial guess
                smoothed = uniform_filter(gray.astype(np.float32), size=16)
                idx = int(np.argmax(smoothed))
                cur_y, cur_x = divmod(idx, width)
                frames.append(CursorFrame(
                    frame_index=0,
                    x=cur_x,
                    y=cur_y,
                    shape=CursorShape.ARROW,
                    confidence=0.5,
                    hidden=False,
                ))
                prev_gray = gray
                frame_index += 1
                continue

            # Stage 1: frame differencing
            diff = np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)).astype(np.uint8)
            candidate_mask = diff > diff_threshold
            candidate_count = int(candidate_mask.sum())

            if candidate_count > 0:
                ys, xs = np.where(candidate_mask)
                cand_x = int(xs.mean())
                cand_y = int(ys.mean())
            else:
                # No motion — keep last position, mark hidden
                frames.append(CursorFrame(
                    frame_index=frame_index,
                    x=cur_x,
                    y=cur_y,
                    shape=CursorShape.ARROW,
                    confidence=0.0,
                    hidden=True,
                ))
                prev_gray = gray
                frame_index += 1
                continue

            # Stage 2: NCC in search window around candidate
            x1 = max(0, cand_x - half_win)
            x2 = min(width, cand_x + half_win)
            y1 = max(0, cand_y - half_win)
            y2 = min(height, cand_y + half_win)
            crop = gray[y1:y2, x1:x2]

            # Resize template to fit crop if crop is smaller
            eff_th = min(tmpl_h, crop.shape[0])
            eff_tw = min(tmpl_w, crop.shape[1])
            eff_tmpl = tmpl[:eff_th, :eff_tw]

            if crop.shape[0] < 2 or crop.shape[1] < 2 or eff_th < 2 or eff_tw < 2:
                score = 0.0
                local_r, local_c = 0, 0
            else:
                score, local_r, local_c = _sliding_ncc(crop, eff_tmpl)

            if score >= ncc_threshold:
                det_x = x1 + local_c
                det_y = y1 + local_r
                cur_x = int(np.clip(det_x, 0, width - 1))
                cur_y = int(np.clip(det_y, 0, height - 1))
                frames.append(CursorFrame(
                    frame_index=frame_index,
                    x=cur_x,
                    y=cur_y,
                    shape=CursorShape.ARROW,
                    confidence=float(np.clip(score, 0.0, 1.0)),
                    hidden=False,
                ))
            else:
                frames.append(CursorFrame(
                    frame_index=frame_index,
                    x=cur_x,
                    y=cur_y,
                    shape=CursorShape.ARROW,
                    confidence=0.0,
                    hidden=True,
                ))

            prev_gray = gray
            frame_index += 1

    finally:
        proc.stdout.close()
        proc.wait()

    return CursorTrack(fps=fps, width=width, height=height, frames=frames)


# ---------------------------------------------------------------------------
# Cursor eraser
# ---------------------------------------------------------------------------

def erase_cursor_region(
    frame_rgb: np.ndarray,    # (H, W, 3) uint8 — NEVER modified in place
    entry: CursorFrame,
    cursor_size: int = 32,
    inpaint_radius: int = 4,
) -> np.ndarray:
    """
    Returns COPY of frame_rgb with cursor region median-inpainted.
    The cursor bounding box is estimated as cursor_size x cursor_size starting at (entry.x, entry.y).
    Inpaint: fill box with median of the surrounding inpaint_radius-pixel ring.
    Clamp all coordinates to frame bounds.
    """
    out = frame_rgb.copy()
    H, W = out.shape[:2]
    x1 = max(0, entry.x)
    y1 = max(0, entry.y)
    x2 = min(W, x1 + cursor_size)
    y2 = min(H, y1 + cursor_size)
    r = inpaint_radius
    rx1 = max(0, x1 - r)
    rx2 = min(W, x2 + r)
    ry1 = max(0, y1 - r)
    ry2 = min(H, y2 + r)
    ring_region = out[ry1:ry2, rx1:rx2].copy()
    inner_mask = np.zeros(ring_region.shape[:2], dtype=bool)
    inner_mask[y1 - ry1:y2 - ry1, x1 - rx1:x2 - rx1] = True
    pixels = ring_region[~inner_mask]  # (N, 3)
    if len(pixels) > 0:
        median_color = np.median(pixels, axis=0).astype(np.uint8)
        out[y1:y2, x1:x2] = median_color
    return out


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import shutil

    # Try PATH first, then the bundled bin/ next to this repo root
    _repo_bin = Path(__file__).parent.parent / 'bin' / 'ffmpeg'
    ffmpeg_bin = shutil.which('ffmpeg') or (str(_repo_bin) if _repo_bin.exists() else None)
    if ffmpeg_bin is None:
        raise RuntimeError('ffmpeg not found in PATH or bin/')

    W, H, FPS, N_FRAMES = 64, 64, 30, 100

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tf:
        vid_path = Path(tf.name)

    try:
        # Generate synthetic video via lavfi testsrc
        subprocess.run(
            [
                ffmpeg_bin, '-y',
                '-f', 'lavfi',
                '-i', f'testsrc=size={W}x{H}:rate={FPS}:duration={N_FRAMES / FPS}',
                '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p',
                str(vid_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        track = detect_cursor_track(
            video_path=vid_path,
            ffmpeg=ffmpeg_bin,
            width=W,
            height=H,
            fps=float(FPS),
        )

        assert len(track.frames) == N_FRAMES, (
            f"Expected {N_FRAMES} frames, got {len(track.frames)}"
        )

        # Encode/decode roundtrip
        encoded = track.encode()
        decoded = CursorTrack.decode(encoded)

        assert decoded.width == track.width
        assert decoded.height == track.height
        assert int(round(decoded.fps)) == int(round(track.fps))
        assert len(decoded.frames) == len(track.frames)

        for orig, rec in zip(track.frames, decoded.frames):
            if orig.hidden:
                assert rec.hidden, f"Frame {orig.frame_index}: hidden mismatch"
            else:
                assert rec.x == orig.x, (
                    f"Frame {orig.frame_index}: x {orig.x} -> {rec.x}"
                )
                assert rec.y == orig.y, (
                    f"Frame {orig.frame_index}: y {orig.y} -> {rec.y}"
                )
                assert rec.shape == orig.shape, (
                    f"Frame {orig.frame_index}: shape {orig.shape} -> {rec.shape}"
                )

        # Verify encode is byte-exact on second pass
        assert CursorTrack.decode(encoded).encode() == encoded, (
            "encode(decode(encode(x))) != encode(x)"
        )

        print('cursor_track OK')

    finally:
        vid_path.unlink(missing_ok=True)
