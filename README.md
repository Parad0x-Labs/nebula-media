# nebula-media 🎬

**Proof-carrying media compression.** Re-encode video with modern codecs — and get a
cryptographic **receipt** that proves *exactly* what was encoded, at what measured
quality, anchored on Solana.

Nebula is scene-aware (it spends bits where the eye actually sees them), dual-codec
(x265 / SVT-AV1), and every encode can mint a verifiable on-chain proof through the
Parad0x stack. Open source, MIT.

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![codecs: x265 · SVT--AV1](https://img.shields.io/badge/codecs-x265%20·%20SVT--AV1-14F195) ![proof: SHA--256 · VMAF · Solana](https://img.shields.io/badge/proof-SHA--256%20·%20VMAF%20·%20Solana-36e0ff)

---

## Why this matters

Video is ~80% of internet traffic and the biggest line on most storage/CDN bills.
Two problems: most encoders spend bits *evenly* (simple scenes cost as much as complex
ones), and *"we compressed it and didn't wreck the quality"* is normally something you
take on faith. Nebula fixes both — **scene-aware bit allocation**, and **a receipt that
proves the quality** instead of asking you to trust it.

## 1 · Scene-aware adaptive encoding

[`nebula/encoder.py`](./nebula/encoder.py) doesn't slap one quality setting on the whole
file. It:

1. **Probes** the source (resolution, fps, bit-depth, grain estimate).
2. **Detects scene cuts** (FFmpeg scene filter).
3. **Builds zones** — complex/high-motion scenes get *more* bits (lower CRF), near-static
   scenes get *fewer*; zone edges snap to keyframe boundaries so no bits are wasted.
4. **Auto-picks the codec** — SVT-AV1 for clean/animated/10-bit, x265 for grainy or short
   clips (or force one). Film-grain synthesis for noisy sources.
5. **Measures VMAF** — mean *and* 1st-percentile (worst ~1% of frames), so quality cliffs
   can't hide behind a good average.
6. **Hashes the output** (SHA-256) for anchoring.

```bash
python -m nebula.encoder input.mp4 --mode balanced --target-vmaf 90
# → output + JSON: vmaf, vmaf_p1, ratio, encoder, zones, proof_hash
```

## 2 · On-chain proof anchoring 🧾 (the stack flex)

This is the part nobody else ships. [`nebula/receipt.py`](./nebula/receipt.py) turns an
encode into a **tamper-evident proof** using the full Parad0x stack:

- Builds a **32-byte commitment** binding the *exact* output bytes + VMAF + ratio + timestamp.
- Compresses the x402 receipt with **[Liquefy](https://github.com/Parad0x-Labs/liquefy)**.
- Archives the receipt blob to **Arweave** (via Irys).
- **Anchors the commitment on Solana mainnet** through the **`receipt_anchor`** program
  (`6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN`) — the same rail [dna-x402](https://github.com/Parad0x-Labs/dna-x402) uses.

Change one byte of the output and the commitment no longer matches. Encoding you can
*verify*, not just trust. (Anchoring is optional — bring your own keypair; without one it
skips gracefully and still gives you the local proof.)

## Reference numbers (reproducible)

All measured on this machine. Commands in [`proof-pack/`](./proof-pack).

### Video — Jellyfish 1080p 60fps (high-motion stress test)

x265 `-preset slow`, libvmaf v0.6.1:

| Source | Output | Ratio | VMAF | Mode |
|---|---|---|---|---|
| 30 MB | **2.43 MB** | **~12×** | **88.1** | safe |
| 30 MB | **2.80 MB** | **10.7×** | **92.95** | CRF 28 medium |

```bash
bash proof-pack/encode_jellyfish_safe.sh   # reproduces within ±0.5 VMAF
```

### Images — Kodak kodim23 (544 KB PNG, SVT-AV1)

| Mode | Output | Ratio | SSIM |
|---|---|---|---|
| safe (CRF 20) | **30 KB** | **18×** | 0.979 |
| balanced (CRF 35) | **14 KB** | **40×** | 0.962 |
| maximum (CRF 50 + grain synthesis) | **7 KB** | **79×** | 0.931 |

SSIM 0.979 = near-identical to the eye. SSIM 0.931 = looks sharp, detail traded for 79× smaller file.

> **Honest notes:** VMAF 88 has minor artifacts on close inspection — not "indistinguishable."
> Jellyfish is the hardest encoding benchmark (60fps, no redundancy). Clean animation and
> talking-head content compresses significantly more. Image ratios depend heavily on content —
> photos with fine grain compress less than synthetic or flat images. All numbers are
> reproducible from the proof-pack scripts.

## What it is — and isn't

- ✅ **Open (MIT)** scene-aware encoder + on-chain proof bridge. All of it is in this repo — no sealed binaries.
- ✅ Standards-based — builds on FFmpeg, x265, SVT-AV1, Opus.
- ❌ **Not a new codec** — it's smarter orchestration of existing ones.
- ❌ **Not lossless** — perceptually close, not bit-identical.
- ⏳ **Image/AVIF + audio pipelines** are early stubs — video is the live path.

## Status

**Alpha.** The scene-aware encoder and the Solana/Liquefy/Arweave proof bridge are
implemented and in the repo today; the reproducible benchmark passes. It hasn't been
hardened across every codec build or scaled to a managed service yet — and we say so.
Every claim here points at code you can read and run.

## Pricing

**Self-hosted: free, forever** (MIT — clone it, run it, own it). A **managed service**
(hosted batch encoding, API, automatic on-chain receipts) is **planned, not yet live** —
this README will say so the moment it ships. The managed service will add hosting and
scale, never close the core.

## 💸 Why people use it

Video is the biggest line on most hosting bills. Nebula shrinks it — and proves the quality held:

- 💾 **Cut storage and CDN cost** — re-encode big libraries far smaller at near-transparent quality.
- 🧾 **Prove the quality** — every encode ships a receipt (VMAF + hashes) you can verify, or anchor on-chain.
- 🎬 **For creators** — smaller uploads, same look; show clients the proof.
- 🆓 **Free and MIT** — self-host the whole pipeline.

*(Alpha; savings are content-dependent — reproduce the numbers in the proof-pack.)*

### How this fits the Parad0x stack

Parad0x Labs builds Web0 on Solana — money and agents that settle themselves. **You are here: 🎬 Media — proof-carrying compression that rides Liquefy and anchors through the x402 rail.**

| Layer | Repo | Does |
|---|---|---|
| 💸 Payments | [dna-x402](https://github.com/Parad0x-Labs/dna-x402) | x402 rail: quote → pay → verify → receipt → anchor |
| 🛠️ Build | [dna-x402-builders](https://github.com/Parad0x-Labs/dna-x402-builders) | Hosted kit: turn any API/bot into a paid agent |
| 🕶️ Privacy | [Dark-Null-Protocol](https://github.com/Parad0x-Labs/Dark-Null-Protocol) | Groth16 privacy settlement, published proofs |
| 🗜️ Data | [liquefy](https://github.com/Parad0x-Labs/liquefy) | Columnar compression that beats Zstd |
| 🛡️ Audit | [liquefy-openclaw-integration](https://github.com/Parad0x-Labs/liquefy-openclaw-integration) | Flight recorder: 24 engines + Solana-anchored audit trails |
| 🎬 Media | **nebula-media** (this repo) | Proof-carrying media compression — scene-aware + on-chain receipts |
| 🧠 Local AI | [nulla-local](https://github.com/Parad0x-Labs/nulla-local) | Local-first agent runtime — your machine, your memory |

Nebula is the stack in one repo: it **encodes**, compresses the receipt with **Liquefy**,
and anchors it through **dna-x402's** `receipt_anchor`. **See it live**: [parad0xlabs.com](https://parad0xlabs.com)

---

**License:** MIT — © 2026 Parad0x Labs
