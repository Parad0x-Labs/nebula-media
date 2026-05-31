# Technical Overview

## The Problem: Media Bloat & Unverifiable Quality
Visual data (4K/8K video, high-res images) dominates infrastructure traffic, and storage
and egress costs scale with it. Two gaps in most encoding workflows: bits are spent
*evenly* regardless of scene complexity, and quality claims are taken on faith.

## The Approach: Scene-Aware + Proof-Carrying
Nebula Media addresses both:

- **Scene-aware bit allocation** (`nebula/encoder.py`): probe → scene-cut detection →
  per-zone CRF (more bits for complex scenes, fewer for static), keyframe-snapped zone
  boundaries, dual-codec auto-selection (x265 / SVT-AV1), film-grain synthesis, and VMAF
  measurement (mean + 1st-percentile).
- **Proof-carrying output** (`nebula/receipt.py`): a 32-byte commitment binds the exact
  output bytes + VMAF + ratio + timestamp; the receipt is compressed with Liquefy,
  archived to Arweave, and the commitment is anchored on Solana mainnet via the
  `receipt_anchor` program (`6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN`).

## Open & Verifiable
- **All MIT** — encoder, proof bridge, pipelines, and verification tools are open in this repo.
- **Offline proof** — quality (VMAF) and integrity (SHA-256) verify locally; your media
  never has to leave your infrastructure.
- **On-chain optional** — anchoring needs your own keypair and skips gracefully without one.

## Reproducibility
Public source clips, public encode scripts, public output references — anyone can re-run
the proof-pack (Jellyfish 1080p, x265, VMAF 88.1, ~2.43 MB, ±0.5 tolerance).

## Status & Alignment
Alpha — the encoder and proof bridge are implemented; image/AVIF and audio are early stubs.
Nebula Media is the media pillar of Parad0x Labs: it encodes, compresses the receipt with
**Liquefy**, and anchors through **dna-x402** — the Web0 stack in one repo.
