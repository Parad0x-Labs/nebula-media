# nebula-media 🎬

**Cut your video files in half without your eyes noticing — and get a receipt that proves it.**

Nebula re-encodes video **scene by scene**, spending bits only where the eye actually sees them. You hit a perceptual-quality target (VMAF) at a far smaller size — and every encode ships a **verifiable receipt** (quality scores + hashes) you can check, or anchor on-chain.

> Perceptual video re-encoding · scene-aware CRF · VMAF quality proofs · open-source (MIT)

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![codecs: x265 · SVT--AV1](https://img.shields.io/badge/codecs-x265%20·%20SVT--AV1-14F195) ![proofs: VMAF · PSNR · SSIM](https://img.shields.io/badge/proofs-VMAF%20·%20PSNR%20·%20SSIM-36e0ff)

---

## Why this matters

Video is **~80% of internet traffic** and the biggest line item on most storage and CDN bills. Almost everyone encodes at a **fixed** quality setting — so simple scenes get the same bitrate as complex ones, and you over-pay on every file.

Nebula fixes the allocation: **simple scenes get fewer bits, complex scenes keep them.** Same perceived quality, smaller file. And because it's not magic, it **proves** it didn't wreck the quality.

## The numbers (real, reproducible)

From the [`proof-pack/`](./proof-pack) in this repo — 1080p Jellyfish test clip, 30s:

| Encoder | Mode | Before | After | Smaller by | VMAF |
|---|---|---|---|---|---|
| **SVT-AV1** | scene-aware | 11.4 MB | 3.9 MB | **−66%** | 93.8 |
| **x265** | scene-aware | 11.4 MB | 4.7 MB | **−59%** | 94.2 |
| x265 | fixed CRF 23 | 11.4 MB | 6.1 MB | −46% | 95.1 |

VMAF ~94 is **visually transparent** — most people can't tell it from the source. Reproduce it yourself:

```bash
bash proof-pack/encode_jellyfish_safe.sh
```

> One 30-second clip is **indicative, not universal.** Savings depend heavily on the source — animation and clean footage compress more; grainy, high-motion footage compresses less.

## How it works

1. **Split** the source into scenes (PySceneDetect).
2. **Score** each scene's complexity (spatial + temporal).
3. **Assign** a per-scene CRF — low CRF (more bits) for complex scenes, high CRF (fewer bits) for simple ones.
4. **Encode** each scene via FFmpeg (**x265** or **SVT-AV1**), concat, re-mux original audio.
5. **Measure** VMAF / PSNR / SSIM against the source.
6. **Receipt** — emit a JSON artifact: input/output hashes, per-scene CRF map, encoder settings, quality scores.

## Proof-carrying encodes 🧾

This is the part nobody else does. Every encode produces a **receipt** — a JSON record of exactly what was encoded, how, and at what measured quality, with hashes. You can verify it locally with [`tools/verify_proof.sh`](./tools), or **anchor it on-chain** through [dna-x402](https://github.com/Parad0x-Labs/dna-x402) for a tamper-evident record. Encoding you don't have to take on trust.

## Quickstart

```bash
pip install -r requirements.txt        # needs FFmpeg + VMAF available on PATH
bash pipelines/av1_pipeline.sh  input.mp4    # or image_pipeline.sh / audio_pipeline.sh
```

Outputs the re-encoded file plus a JSON receipt. See [`docs/overview.md`](./docs/overview.md) and [`docs/methodology.md`](./docs/methodology.md) for the full walkthrough.

## How it compares

| Capability | Nebula | Typical SaaS encoder |
|---|---|---|
| Scene-aware CRF | ✅ | sometimes |
| Verifiable quality receipt | ✅ VMAF/PSNR/SSIM + hashes | rare |
| Open-source core | ✅ MIT | ❌ |
| Self-hostable | ✅ | usually not |
| On-chain anchoring | ✅ optional (Parad0x) | ❌ |

## What it is — and isn't

- ✅ A scene-aware **orchestration layer** over FFmpeg + open codecs, with verifiable receipts.
- ❌ **Not a new codec** or compression format — it's smarter use of x265 / SVT-AV1.
- ❌ **Not lossless** — output is perceptually close, not bit-identical.
- ❌ **Not magic** — savings depend on the source.

## Pricing

**Self-hosted: free, forever** (MIT — clone it, run it, own it). A **managed service** (hosted encoding, batch/API, automatic on-chain receipts) is **planned, not yet live** — this README will say so the moment it's real.

## Status

**Alpha.** The core pipeline works and is reproducible today. It hasn't been hardened for every edge case or scaled to a managed service yet. We say so out loud — every claim here ships with code you can run.

### How this fits the Parad0x stack

Parad0x Labs builds Web0 on Solana — money and agents that settle themselves. **You are here: 🎬 Media.**

| Layer | Repo | Does |
|---|---|---|
| 💸 Payments | [dna-x402](https://github.com/Parad0x-Labs/dna-x402) | x402 rail: quote → pay → verify → receipt → anchor |
| 🛠️ Build | [dna-x402-builders](https://github.com/Parad0x-Labs/dna-x402-builders) | Hosted kit: turn any API/bot into a paid agent |
| 🕶️ Privacy | [Dark-Null-Protocol](https://github.com/Parad0x-Labs/Dark-Null-Protocol) | Groth16 privacy settlement, published proofs |
| 🗜️ Data | [liquefy](https://github.com/Parad0x-Labs/liquefy) | Columnar compression that beats Zstd + audit trails |
| 🎬 Media | **nebula-media** (this repo) | Perceptual video re-encoding, VMAF quality proofs |
| 🧠 Local AI | [nulla-local](https://github.com/Parad0x-Labs/nulla-local) | Local-first agent runtime — your machine, your memory |

**See it live**: [parad0xlabs.com](https://parad0xlabs.com)

---

**License:** MIT — © 2026 Parad0x Labs
