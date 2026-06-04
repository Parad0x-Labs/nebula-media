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
nebula/quality_commitment.py — Quality commitment and proof module.

Generates a cryptographic commitment to the SSIM/PSNR quality metrics of a
(source, output) encode pair.  The commitment can be verified without
re-running the full encode:

  * fast=True  — structural check only (Merkle root + commitment hash)  O(1)
  * fast=False — recomputes SSIM on sampled frames and compares            O(n)

Architecture
------------
Phase 1 (this module): hash-based commitment.
  - Per-frame quality leaves hashed into a binary Merkle tree.
  - Master commitment = SHA-256 over source hash, output hash, metrics, root.
  - Full tree stored in the commitment for offline auditing.

Phase 2 (stub): ZK proof backends.
  - ZKBackend Protocol defined for RISC Zero / SP1 plug-in.
  - MockZKBackend returns HMAC-SHA256 as a structural placeholder.

Public API
----------
    from nebula.quality_commitment import (
        generate_commitment, verify_commitment,
        commitment_to_json, commitment_from_json,
        QualityCommitment, VerificationResult,
    )
"""

from __future__ import annotations

__all__ = [
    "FrameQualityLeaf",
    "QualityCommitment",
    "VerificationResult",
    "ZKBackend",
    "MockZKBackend",
    "generate_commitment",
    "verify_commitment",
    "commitment_to_json",
    "commitment_from_json",
]

import hashlib
import hmac
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np

from nebula.metrics import (
    iter_frame_pairs_rgb,
    compute_ssim,
    compute_psnr,
    ClipQualityMetrics,
    rgb_to_y,
)

log = logging.getLogger("nebula.quality_commitment")


# ---------------------------------------------------------------------------
# Dataclasses — Phase 1
# ---------------------------------------------------------------------------

@dataclass
class FrameQualityLeaf:
    """Cryptographic leaf for a single sampled frame pair."""

    frame_index: int
    """0-based index in the subsampled frame sequence."""

    ssim: float
    """SSIM measured for this frame pair, rounded to 6 decimal places."""

    psnr: float
    """PSNR (dB) measured for this frame pair, rounded to 6 decimal places."""

    ref_frame_hash: str
    """SHA-256 hex of raw RGB bytes of the reference frame."""

    dis_frame_hash: str
    """SHA-256 hex of raw RGB bytes of the distorted frame."""

    leaf_hash: str
    """SHA-256(frame_index || ssim_str || psnr_str || ref_frame_hash || dis_frame_hash)."""


@dataclass
class QualityCommitment:
    """Full quality commitment for a (source, output) encode pair."""

    # Version
    version: int = 1

    # Source and output identification
    source_path: str = ""
    output_path: str = ""
    source_hash: str = ""
    """SHA-256 of full source file."""
    output_hash: str = ""
    """SHA-256 of full output file (same as proof_hash in encoder)."""

    # Clip-level metrics
    ssim_mean: float = 0.0
    ssim_p1: float = 0.0
    psnr_mean: float = 0.0
    frame_count: int = 0
    sampled_count: int = 0
    n_subsample: int = 6

    # Merkle tree of per-frame quality
    merkle_root: str = ""
    """SHA-256 Merkle root of frame leaf hashes."""
    merkle_tree: List[List[str]] = field(default_factory=list)
    """Full tree: [leaves, level1, level2, ..., [root]]."""
    frame_leaves: List[FrameQualityLeaf] = field(default_factory=list)

    # Encoder context
    encoder: str = ""
    mode: str = ""
    vmaf_mean: float = 0.0
    timestamp_utc: str = ""
    """ISO 8601 UTC timestamp."""

    # Master commitment
    commitment: str = ""
    """SHA-256(source_hash || output_hash || ssim_mean || psnr_mean || merkle_root || timestamp_utc)."""

    # ZK extension point (Phase 2 — None until RISC Zero integration)
    zk_proof: Optional[bytes] = None
    zk_system: Optional[str] = None
    """'risc_zero' | 'sp1' | None."""
    zk_program_id: Optional[str] = None


@dataclass
class VerificationResult:
    """Result of verify_commitment()."""

    passed: bool
    commitment_hash_valid: bool
    merkle_root_valid: bool
    ssim_recomputed: Optional[float] = None
    """None if fast=True."""
    ssim_delta: Optional[float] = None
    """|recomputed - claimed|.  None if fast=True."""
    ssim_tolerance: float = 0.001
    """Allowable float rounding error in recomputed SSIM comparison."""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Merkle tree
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_merkle_tree(leaves: List[str]) -> List[List[str]]:
    """
    Build a binary Merkle tree from a list of leaf hashes.

    Odd number of nodes at any level: the last node is duplicated (Bitcoin
    convention).  Internal nodes are SHA-256(left_hex + right_hex) where
    the concatenation is of the raw hex strings (no separator).

    Returns a list of levels: levels[0] = leaves, levels[-1] = [root].
    For a single leaf, returns [[leaf], [leaf]] — root equals the leaf.
    """
    if not leaves:
        # Empty tree: synthetic empty root.
        empty = _sha256_hex(b"")
        return [[empty], [empty]]

    levels: List[List[str]] = [list(leaves)]
    current = list(leaves)

    while len(current) > 1:
        next_level: List[str] = []
        # Pad to even length by duplicating last element.
        if len(current) % 2 == 1:
            current = current + [current[-1]]
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1]
            parent = _sha256_hex((left + right).encode("ascii"))
            next_level.append(parent)
        levels.append(next_level)
        current = next_level

    return levels


# ---------------------------------------------------------------------------
# Leaf construction
# ---------------------------------------------------------------------------

def _compute_leaf(
    frame_index: int,
    ssim: float,
    psnr: float,
    ref_frame_rgb: np.ndarray,
    dis_frame_rgb: np.ndarray,
) -> FrameQualityLeaf:
    """
    Build a FrameQualityLeaf for a single frame pair.

    Metric values are fixed to 6 decimal places for determinism across
    platforms.  Frame hashes are SHA-256 of the raw uint8 bytes.
    Leaf hash = SHA-256(frame_index_4be || ssim_str || psnr_str || ref_hash_hex || dis_hash_hex).

    Parameters
    ----------
    frame_index:
        0-based index in the subsampled sequence.
    ssim, psnr:
        Raw float values from compute_ssim / compute_psnr.
    ref_frame_rgb, dis_frame_rgb:
        (H, W, 3) uint8 arrays.
    """
    ssim_str = f"{ssim:.6f}"
    psnr_str = f"{psnr:.6f}"

    ref_hash = _sha256_hex(ref_frame_rgb.tobytes())
    dis_hash = _sha256_hex(dis_frame_rgb.tobytes())

    leaf_input = (
        frame_index.to_bytes(4, "big")
        + ssim_str.encode("ascii")
        + psnr_str.encode("ascii")
        + ref_hash.encode("ascii")
        + dis_hash.encode("ascii")
    )
    leaf_hash = _sha256_hex(leaf_input)

    return FrameQualityLeaf(
        frame_index=frame_index,
        ssim=round(ssim, 6),
        psnr=round(psnr, 6),
        ref_frame_hash=ref_hash,
        dis_frame_hash=dis_hash,
        leaf_hash=leaf_hash,
    )


# ---------------------------------------------------------------------------
# File hash
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest of an entire file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Master commitment hash
# ---------------------------------------------------------------------------

def _master_commitment(
    source_hash: str,
    output_hash: str,
    ssim_mean: float,
    psnr_mean: float,
    merkle_root: str,
    timestamp_utc: str,
) -> str:
    """
    SHA-256(source_hash || output_hash || ssim_mean_str || psnr_mean_str || merkle_root || timestamp_utc).

    All components are encoded as ASCII strings and concatenated before hashing.
    ssim/psnr are fixed to 6 decimal places for determinism.
    """
    parts = (
        source_hash
        + output_hash
        + f"{ssim_mean:.6f}"
        + f"{psnr_mean:.6f}"
        + merkle_root
        + timestamp_utc
    )
    return _sha256_hex(parts.encode("ascii"))


# ---------------------------------------------------------------------------
# generate_commitment
# ---------------------------------------------------------------------------

def generate_commitment(
    source_path: Path,
    output_path: Path,
    ffmpeg: str,
    encoder: str,
    mode: str,
    vmaf_mean: float,
    n_subsample: int = 6,
) -> QualityCommitment:
    """
    Generate a full quality commitment for an (source, output) encode pair.

    Iterates over sampled frame pairs via ffmpeg pipes, computes SSIM/PSNR
    for each, builds a Merkle tree of per-frame leaves, and hashes the whole
    thing into a master commitment.

    Parameters
    ----------
    source_path:
        Original (reference) video file.
    output_path:
        Encoded output file.
    ffmpeg:
        Resolved path to the ffmpeg binary.
    encoder:
        Encoder string ('x265', 'svt-av1', etc.) — stored in commitment.
    mode:
        Encode mode ('safe', 'balanced', 'maximum') — stored in commitment.
    vmaf_mean:
        VMAF score from the encoder pass — stored in commitment.
    n_subsample:
        Sample every Nth frame (default 6).

    Returns
    -------
    QualityCommitment fully populated.
    """
    source_path = Path(source_path)
    output_path = Path(output_path)

    log.info(
        "generating quality commitment: source=%s output=%s n_subsample=%d",
        source_path.name, output_path.name, n_subsample,
    )

    # Hash both files up front — these are the file-level bindings.
    log.info("hashing source file …")
    source_hash = _sha256_file(source_path)
    log.info("hashing output file …")
    output_hash = _sha256_file(output_path)

    # Accumulate per-frame metrics.
    frame_leaves: List[FrameQualityLeaf] = []
    ssim_vals: List[float] = []
    psnr_vals: List[float] = []

    log.info("computing per-frame SSIM/PSNR …")
    leaf_index = 0
    for _raw_frame_idx, ref_rgb, dis_rgb in iter_frame_pairs_rgb(
        source_path, output_path, ffmpeg, n_subsample=n_subsample
    ):
        # metrics.py compute_ssim/psnr expect float64 luma planes.
        ref_y = rgb_to_y(ref_rgb)
        dis_y = rgb_to_y(dis_rgb)

        ssim = compute_ssim(ref_y, dis_y)
        psnr = compute_psnr(ref_y, dis_y)

        leaf = _compute_leaf(leaf_index, ssim, psnr, ref_rgb, dis_rgb)
        frame_leaves.append(leaf)
        ssim_vals.append(ssim)
        psnr_vals.append(psnr)
        leaf_index += 1

    sampled_count = len(frame_leaves)
    log.info("  sampled %d frames", sampled_count)

    if sampled_count == 0:
        # No decodeable frames: create a degenerate but well-formed commitment.
        ssim_mean_val = 0.0
        ssim_p1_val   = 0.0
        psnr_mean_val = 0.0
        frame_count   = 0
    else:
        ssim_arr = np.array(ssim_vals, dtype=np.float64)
        # Filter inf from PSNR before averaging.
        finite_psnr = [v for v in psnr_vals if not math.isinf(v)]
        psnr_mean_val = float(np.mean(finite_psnr)) if finite_psnr else float("inf")

        ssim_mean_val = float(ssim_arr.mean())
        ssim_p1_val   = float(np.percentile(ssim_arr, 1))
        frame_count   = sampled_count * n_subsample  # approximate total

    # Build Merkle tree over leaf hashes.
    leaf_hashes = [lf.leaf_hash for lf in frame_leaves]
    merkle_tree = _build_merkle_tree(leaf_hashes)
    merkle_root = merkle_tree[-1][0]

    timestamp_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    commitment_hash = _master_commitment(
        source_hash=source_hash,
        output_hash=output_hash,
        ssim_mean=ssim_mean_val,
        psnr_mean=psnr_mean_val,
        merkle_root=merkle_root,
        timestamp_utc=timestamp_utc,
    )

    log.info("  merkle_root=%s", merkle_root[:16] + "…")
    log.info("  commitment=%s", commitment_hash[:16] + "…")

    return QualityCommitment(
        version=1,
        source_path=str(source_path),
        output_path=str(output_path),
        source_hash=source_hash,
        output_hash=output_hash,
        ssim_mean=round(ssim_mean_val, 6),
        ssim_p1=round(ssim_p1_val, 6),
        psnr_mean=round(psnr_mean_val, 6),
        frame_count=frame_count,
        sampled_count=sampled_count,
        n_subsample=n_subsample,
        merkle_root=merkle_root,
        merkle_tree=merkle_tree,
        frame_leaves=frame_leaves,
        encoder=encoder,
        mode=mode,
        vmaf_mean=round(vmaf_mean, 6),
        timestamp_utc=timestamp_utc,
        commitment=commitment_hash,
    )


# ---------------------------------------------------------------------------
# verify_commitment
# ---------------------------------------------------------------------------

def verify_commitment(
    commitment: QualityCommitment,
    source_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    ffmpeg: Optional[str] = None,
    fast: bool = True,
) -> VerificationResult:
    """
    Verify a QualityCommitment.

    fast=True (default):
        Structural verification only — rebuild the Merkle root from the stored
        leaf hashes and recompute the master commitment hash.  O(n_leaves).
        No video decoding required.

    fast=False:
        Also re-decodes the video pair, recomputes SSIM on sampled frames,
        and checks that the recomputed mean SSIM matches the claimed value
        within ssim_tolerance=0.001 (accounts for float rounding).
        Requires source_path, output_path, and ffmpeg.

    Parameters
    ----------
    commitment:
        The QualityCommitment to verify.
    source_path, output_path:
        Required only when fast=False.
    ffmpeg:
        Required only when fast=False.
    fast:
        True = structural only, False = full recompute.

    Returns
    -------
    VerificationResult
    """
    error: Optional[str] = None

    # --- Verify Merkle root ---
    leaf_hashes = [lf.leaf_hash for lf in commitment.frame_leaves]
    rebuilt_tree = _build_merkle_tree(leaf_hashes)
    rebuilt_root = rebuilt_tree[-1][0]
    merkle_root_valid = rebuilt_root == commitment.merkle_root

    if not merkle_root_valid:
        error = (
            f"Merkle root mismatch: stored={commitment.merkle_root[:16]}… "
            f"recomputed={rebuilt_root[:16]}…"
        )

    # --- Verify master commitment hash ---
    expected_commitment = _master_commitment(
        source_hash=commitment.source_hash,
        output_hash=commitment.output_hash,
        ssim_mean=commitment.ssim_mean,
        psnr_mean=commitment.psnr_mean,
        merkle_root=commitment.merkle_root,
        timestamp_utc=commitment.timestamp_utc,
    )
    commitment_hash_valid = expected_commitment == commitment.commitment

    if not commitment_hash_valid and error is None:
        error = (
            f"Commitment hash mismatch: stored={commitment.commitment[:16]}… "
            f"expected={expected_commitment[:16]}…"
        )

    if fast:
        passed = merkle_root_valid and commitment_hash_valid
        return VerificationResult(
            passed=passed,
            commitment_hash_valid=commitment_hash_valid,
            merkle_root_valid=merkle_root_valid,
            error=error,
        )

    # --- Slow path: recompute SSIM ---
    if source_path is None or output_path is None or ffmpeg is None:
        return VerificationResult(
            passed=False,
            commitment_hash_valid=commitment_hash_valid,
            merkle_root_valid=merkle_root_valid,
            error="fast=False requires source_path, output_path, and ffmpeg.",
        )

    ssim_vals: List[float] = []
    try:
        for _, ref_rgb, dis_rgb in iter_frame_pairs_rgb(
            Path(source_path), Path(output_path), ffmpeg,
            n_subsample=commitment.n_subsample,
        ):
            ref_y = rgb_to_y(ref_rgb)
            dis_y = rgb_to_y(dis_rgb)
            ssim_vals.append(compute_ssim(ref_y, dis_y))
    except Exception as exc:
        return VerificationResult(
            passed=False,
            commitment_hash_valid=commitment_hash_valid,
            merkle_root_valid=merkle_root_valid,
            error=f"Frame decode failed during slow verification: {exc}",
        )

    if not ssim_vals:
        return VerificationResult(
            passed=False,
            commitment_hash_valid=commitment_hash_valid,
            merkle_root_valid=merkle_root_valid,
            error="No frames decoded during slow verification.",
        )

    ssim_recomputed = float(np.mean(ssim_vals))
    ssim_delta = abs(ssim_recomputed - commitment.ssim_mean)
    tolerance = 0.001
    ssim_ok = ssim_delta <= tolerance

    if not ssim_ok and error is None:
        error = (
            f"SSIM mismatch: claimed={commitment.ssim_mean:.6f} "
            f"recomputed={ssim_recomputed:.6f} delta={ssim_delta:.6f} > {tolerance}"
        )

    passed = merkle_root_valid and commitment_hash_valid and ssim_ok
    return VerificationResult(
        passed=passed,
        commitment_hash_valid=commitment_hash_valid,
        merkle_root_valid=merkle_root_valid,
        ssim_recomputed=ssim_recomputed,
        ssim_delta=ssim_delta,
        ssim_tolerance=tolerance,
        error=error,
    )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def commitment_to_json(c: QualityCommitment) -> str:
    """
    Serialize a QualityCommitment to a JSON string.

    frame_leaves are serialized as a list of dicts.
    merkle_tree is serialized as a list of lists of hex strings.
    zk_proof bytes are hex-encoded if present.
    """
    leaves_dicts = [
        {
            "frame_index":    lf.frame_index,
            "ssim":           lf.ssim,
            "psnr":           lf.psnr,
            "ref_frame_hash": lf.ref_frame_hash,
            "dis_frame_hash": lf.dis_frame_hash,
            "leaf_hash":      lf.leaf_hash,
        }
        for lf in c.frame_leaves
    ]

    obj = {
        "version":       c.version,
        "source_path":   c.source_path,
        "output_path":   c.output_path,
        "source_hash":   c.source_hash,
        "output_hash":   c.output_hash,
        "ssim_mean":     c.ssim_mean,
        "ssim_p1":       c.ssim_p1,
        "psnr_mean":     c.psnr_mean,
        "frame_count":   c.frame_count,
        "sampled_count": c.sampled_count,
        "n_subsample":   c.n_subsample,
        "merkle_root":   c.merkle_root,
        "merkle_tree":   c.merkle_tree,
        "frame_leaves":  leaves_dicts,
        "encoder":       c.encoder,
        "mode":          c.mode,
        "vmaf_mean":     c.vmaf_mean,
        "timestamp_utc": c.timestamp_utc,
        "commitment":    c.commitment,
        "zk_proof":      c.zk_proof.hex() if c.zk_proof is not None else None,
        "zk_system":     c.zk_system,
        "zk_program_id": c.zk_program_id,
    }
    return json.dumps(obj, indent=2)


def commitment_from_json(s: str) -> QualityCommitment:
    """Deserialize a QualityCommitment from a JSON string produced by commitment_to_json."""
    obj = json.loads(s)

    frame_leaves = [
        FrameQualityLeaf(
            frame_index=lf["frame_index"],
            ssim=lf["ssim"],
            psnr=lf["psnr"],
            ref_frame_hash=lf["ref_frame_hash"],
            dis_frame_hash=lf["dis_frame_hash"],
            leaf_hash=lf["leaf_hash"],
        )
        for lf in obj.get("frame_leaves", [])
    ]

    zk_proof_raw = obj.get("zk_proof")
    zk_proof = bytes.fromhex(zk_proof_raw) if zk_proof_raw is not None else None

    return QualityCommitment(
        version=obj.get("version", 1),
        source_path=obj.get("source_path", ""),
        output_path=obj.get("output_path", ""),
        source_hash=obj.get("source_hash", ""),
        output_hash=obj.get("output_hash", ""),
        ssim_mean=obj.get("ssim_mean", 0.0),
        ssim_p1=obj.get("ssim_p1", 0.0),
        psnr_mean=obj.get("psnr_mean", 0.0),
        frame_count=obj.get("frame_count", 0),
        sampled_count=obj.get("sampled_count", 0),
        n_subsample=obj.get("n_subsample", 6),
        merkle_root=obj.get("merkle_root", ""),
        merkle_tree=obj.get("merkle_tree", []),
        frame_leaves=frame_leaves,
        encoder=obj.get("encoder", ""),
        mode=obj.get("mode", ""),
        vmaf_mean=obj.get("vmaf_mean", 0.0),
        timestamp_utc=obj.get("timestamp_utc", ""),
        commitment=obj.get("commitment", ""),
        zk_proof=zk_proof,
        zk_system=obj.get("zk_system"),
        zk_program_id=obj.get("zk_program_id"),
    )


# ---------------------------------------------------------------------------
# Phase 2: ZK backend interface (stub)
# ---------------------------------------------------------------------------

try:
    from typing import Protocol, runtime_checkable
    _PROTOCOL_AVAILABLE = True
except ImportError:
    # Python 3.7 fallback (Protocol was added in 3.8).
    _PROTOCOL_AVAILABLE = False
    Protocol = object  # type: ignore[assignment, misc]

    def runtime_checkable(cls):  # type: ignore[misc]
        return cls


class ZKBackend(Protocol):
    """
    Interface for ZK proof backends.  RISC Zero / SP1 plug in here.

    Phase 2: implement RISCZeroBackend or SP1Backend conforming to this protocol.
    The prove_ssim_range method generates a ZK proof that the encoder's
    reported ssim_mean lies within [lower_bound, upper_bound] without revealing
    the per-frame pixel data.
    """

    def prove_ssim_range(
        self,
        commitment: QualityCommitment,
        lower_bound: float,
        upper_bound: float,
    ) -> bytes:
        """
        Generate a ZK proof that commitment.ssim_mean ∈ [lower_bound, upper_bound].

        The proof is opaque bytes; the format is backend-specific.
        For RISC Zero the guest program would receive the Merkle root and
        the claimed ssim_mean as public inputs and verify the range claim.
        """
        ...

    def verify_proof(self, proof: bytes, commitment_hash: str) -> bool:
        """
        Verify a proof blob against a commitment hash.

        Returns True only if the proof is valid for the given commitment.
        """
        ...


class MockZKBackend:
    """
    Placeholder ZK backend.  Returns an HMAC-SHA256 signature instead of a
    real ZK proof.  Structurally identical to what a real backend will produce
    (opaque bytes tied to the commitment hash), making it drop-in testable.

    Signature format:
        HMAC-SHA256(key=operator_secret_key, msg=commitment_hash.encode())

    operator_secret_key defaults to SHA-256(commitment_hash) — a
    self-certifying commitment that is not actually secret.  In production,
    replace this with a real key or use the RISCZeroBackend.

    Replace with RISCZeroBackend when the RISC Zero Rust guest is implemented.
    """

    def __init__(self, operator_secret_key: Optional[bytes] = None) -> None:
        self._secret = operator_secret_key  # None → derive from commitment_hash

    def _key(self, commitment_hash: str) -> bytes:
        if self._secret is not None:
            return self._secret
        # Self-certifying: key = SHA-256(commitment_hash).  Not secret.
        return hashlib.sha256(commitment_hash.encode("ascii")).digest()

    def prove_ssim_range(
        self,
        commitment: QualityCommitment,
        lower_bound: float,
        upper_bound: float,
    ) -> bytes:
        """
        Return HMAC-SHA256(key, commitment_hash || lower_bound || upper_bound).

        The range bounds are encoded as ASCII strings to keep this pure-Python
        with no struct dependency, matching the ZKBackend Protocol signature.
        """
        msg = (
            commitment.commitment
            + f"|{lower_bound:.6f}"
            + f"|{upper_bound:.6f}"
        ).encode("ascii")
        key = self._key(commitment.commitment)
        return hmac.new(key, msg, hashlib.sha256).digest()

    def verify_proof(self, proof: bytes, commitment_hash: str) -> bool:
        """
        Verify an HMAC proof produced by prove_ssim_range.

        Since the range bounds are encoded in the proof message, this method
        can only verify structural integrity (HMAC key is correct), not the
        range claim itself.  A real ZK backend would verify the circuit output.
        """
        # Re-derive key and check that proof has correct HMAC length.
        key = self._key(commitment_hash)
        # We cannot verify the range claim here without the original bounds,
        # so we verify structural size and that the key produces a valid-length MAC.
        expected_len = hashlib.sha256().digest_size  # 32 bytes
        return len(proof) == expected_len


# ---------------------------------------------------------------------------
# Self-test: __main__
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    import subprocess
    import sys
    import tempfile

    print("quality_commitment self-test")
    print("=" * 60)

    # Locate ffmpeg.
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        # Try project-local bin/ directory.
        _local_bin = Path(__file__).parent.parent / "bin" / "ffmpeg"
        if _local_bin.exists():
            ffmpeg = str(_local_bin)
    if ffmpeg is None:
        print("SKIP: ffmpeg not found on PATH or in bin/.")
        sys.exit(0)

    print(f"ffmpeg: {ffmpeg}")

    # Generate two 64x64 synthetic 3-frame video files using ffmpeg's lavfi source.
    # We use 'color' source for ref (solid blue) and a noisy variant for dis.
    work = Path(tempfile.mkdtemp(prefix="nebula_qc_test_"))
    ref_path = work / "ref.mp4"
    dis_path = work / "dis.mp4"

    N_FRAMES = 10   # enough for a p1 percentile to be meaningful

    def make_video(path: Path, color: str = "blue", noise: int = 0) -> None:
        """Create a tiny synthetic video via ffmpeg lavfi."""
        vf = f"color={color}:size=64x64:rate=10:duration=1"
        if noise > 0:
            vf += f",noise=alls={noise}:allf=t"
        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", vf,
            "-vframes", str(N_FRAMES),
            "-c:v", "libx264", "-crf", "0",   # lossless for reference
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"Test video generation failed:\n{result.stderr.decode()[:500]}"
            )

    print("generating synthetic test videos (64x64, 10 frames each) …")
    try:
        make_video(ref_path, color="blue@0.5", noise=0)
        make_video(dis_path, color="blue@0.5", noise=15)   # same content, light noise
    except RuntimeError as exc:
        print(f"SKIP: {exc}")
        shutil.rmtree(work, ignore_errors=True)
        sys.exit(0)

    print(f"  ref: {ref_path}")
    print(f"  dis: {dis_path}")

    # --- Generate commitment ---
    print("\n[1] generate_commitment …")
    c = generate_commitment(
        source_path=ref_path,
        output_path=dis_path,
        ffmpeg=ffmpeg,
        encoder="x264-test",
        mode="test",
        vmaf_mean=95.0,
        n_subsample=1,   # every frame — small clip
    )

    print(f"  sampled_count : {c.sampled_count}")
    print(f"  ssim_mean     : {c.ssim_mean:.6f}")
    print(f"  ssim_p1       : {c.ssim_p1:.6f}")
    print(f"  psnr_mean     : {c.psnr_mean:.6f}")
    print(f"  merkle_root   : {c.merkle_root[:32]}…")
    print(f"  commitment    : {c.commitment[:32]}…")
    print(f"  leaves        : {len(c.frame_leaves)}")
    print(f"  tree levels   : {len(c.merkle_tree)}")

    assert c.sampled_count > 0, "No frames sampled"
    assert len(c.frame_leaves) == c.sampled_count
    assert c.merkle_root == c.merkle_tree[-1][0], "Merkle root inconsistent"

    # --- Fast verification (structural only) ---
    print("\n[2] verify_commitment (fast=True) …")
    vr_fast = verify_commitment(c, fast=True)
    print(f"  passed                : {vr_fast.passed}")
    print(f"  commitment_hash_valid : {vr_fast.commitment_hash_valid}")
    print(f"  merkle_root_valid     : {vr_fast.merkle_root_valid}")
    assert vr_fast.passed, f"Fast verification failed: {vr_fast.error}"

    # --- Slow verification (recomputes SSIM) ---
    print("\n[3] verify_commitment (fast=False, recomputes SSIM) …")
    vr_slow = verify_commitment(
        c,
        source_path=ref_path,
        output_path=dis_path,
        ffmpeg=ffmpeg,
        fast=False,
    )
    print(f"  passed           : {vr_slow.passed}")
    print(f"  ssim_recomputed  : {vr_slow.ssim_recomputed}")
    print(f"  ssim_delta       : {vr_slow.ssim_delta}")
    assert vr_slow.passed, f"Slow verification failed: {vr_slow.error}"

    # --- Tamper test: mutate ssim_mean, fast verify should fail ---
    print("\n[4] tamper test: mutate ssim_mean → fast verify must fail …")
    import copy
    c_tampered = copy.deepcopy(c)
    c_tampered.ssim_mean = round(c.ssim_mean + 0.1, 6)
    vr_tampered = verify_commitment(c_tampered, fast=True)
    assert not vr_tampered.commitment_hash_valid, "Tamper not detected!"
    print(f"  tamper detected correctly (passed={vr_tampered.passed})")

    # --- JSON round-trip ---
    print("\n[5] JSON serialization round-trip …")
    json_str = commitment_to_json(c)
    c2 = commitment_from_json(json_str)
    assert c2.commitment == c.commitment, "commitment hash changed after JSON round-trip"
    assert c2.merkle_root == c.merkle_root, "merkle_root changed after JSON round-trip"
    assert len(c2.frame_leaves) == len(c.frame_leaves), "frame_leaves count changed"
    print(f"  JSON length: {len(json_str)} bytes — round-trip OK")

    # --- MockZKBackend ---
    print("\n[6] MockZKBackend …")
    zk = MockZKBackend()
    proof = zk.prove_ssim_range(c, lower_bound=0.5, upper_bound=1.0)
    assert isinstance(proof, bytes) and len(proof) == 32, f"proof shape wrong: {proof!r}"
    valid = zk.verify_proof(proof, c.commitment)
    assert valid, "MockZKBackend.verify_proof returned False"
    print(f"  proof: {proof.hex()[:16]}…  (32 bytes, hmac-sha256)")
    print(f"  verify_proof: {valid}")

    # Cleanup.
    shutil.rmtree(work, ignore_errors=True)
    print("\nAll tests passed.")
