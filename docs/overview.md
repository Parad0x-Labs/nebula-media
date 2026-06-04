# Technical Overview

## What it is

Nebula is a scene-aware video encoder and proof bridge. Drop a file, get a compressed output and a cryptographic receipt that proves what quality you actually shipped — without having to trust anyone's word for it.

Two problems it solves:
- **Media bloat**: most encoders spend bits evenly regardless of scene complexity. Nebula probes each clip, finds cut boundaries, and allocates bits per zone.
- **Unverifiable quality**: "we compressed it and kept quality" is normally a claim. Nebula produces a SHA-256 + VMAF receipt you can verify locally, or anchor on Solana.

## Encoder pipeline

`nebula/encoder.py` runs in one command:

```
probe → content detection → scene cuts → zone CRFs → encode → VMAF → proof hash
```

**Content detection.** The encoder probes grain level, resolution, and source codec to classify content automatically:
- Screen recordings / UI / text → x265 with screen preset (no-sao, no-strong-intra-smoothing, tskip=1)
- High-grain / film content → x265 (AV1 film-grain synthesis is unreliable above grain_level 0.5)
- Everything else → SVT-AV1 (faster than x265 on long content, smaller at VMAF 95+)

**Scene zones.** ffmpeg scene-cut detection builds per-zone CRF. Frames inside a complex cut get more bits; near-static zones get fewer. Zone boundaries snap to keyframe intervals — no wasted bits on mid-GOP cuts. Single-zone clips fold the CRF offset into the global flag (avoids x265 4.0 zones= parsing bug).

**Dual codec + VVC.** Four encoder paths:
- `x265` — HEVC, widest device compatibility, best for grain/screen content
- `svt-av1` — AV1, faster than x265 on clean content, smaller at equal VMAF
- `vvc` — H.266/VVC via libvvenc, ~25% smaller than HEVC at equal VMAF (archival, slow on current Apple Silicon builds)
- `videotoolbox` — Apple hardware HEVC, real-time on M-series, draft/preview path

**VMAF.** Measured after every encode unless skipped with `--no-vmaf`. Uses `vmaf_v0.6.1` for HD content, `vmaf_4k_v0.6.1` for 4K/UHD (auto-selected). Reports mean and 1st-percentile (worst ~1% of frames).

**Apple Silicon optimisation.** On M-series Macs, x265 is tuned to run `frame-threads=P_CORES` (e.g. 4 on M4) so frame-level work lands on P-cores; WPP row parallelism fills in E-cores via `pools=+`. Measured on M4: 874% avg CPU utilisation across 10 cores (87% of total capacity).

## Proof pipeline

`nebula/receipt.py` turns an encode into a tamper-evident proof:
1. Builds a 32-byte commitment: SHA-256(output bytes) + VMAF + ratio + timestamp
2. Compresses the receipt with [Liquefy](https://github.com/Parad0x-Labs/liquefy)
3. Archives to Arweave via Irys
4. Anchors the commitment on Solana mainnet via `receipt_anchor` (`6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN`) — the same rail [dna-x402](https://github.com/Parad0x-Labs/dna-x402) uses

Anchoring is optional. Without a keypair it skips gracefully and still produces the local SHA-256 proof.

## Status

Alpha. The encoder pipeline and proof bridge are implemented. Image and audio pipelines are early stubs. VVC is functional but encode speed on Apple Silicon arm64 is ~10-20× slower than x265 until libvvenc gets NEON optimisation.
