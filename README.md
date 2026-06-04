# nebula-media 🎬

**Proof-carrying media compression.** Scene-aware encoding across four codecs — with a cryptographic receipt that proves exactly what quality you shipped, anchored on Solana.

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![codecs: x265·AV1·VVC·VideoToolbox](https://img.shields.io/badge/codecs-x265%20·%20AV1%20·%20VVC%20·%20VideoToolbox-14F195) ![proof: SHA--256·VMAF·Solana](https://img.shields.io/badge/proof-SHA--256%20·%20VMAF%20·%20Solana-36e0ff)

---

## Quick start

```bash
# balanced mode — auto-selects codec, measures VMAF, outputs proof hash
python -m nebula.encoder input.mp4

# force a specific encoder
python -m nebula.encoder input.mp4 --encoder vvc --mode safe
python -m nebula.encoder input.mp4 --encoder videotoolbox --mode balanced  # hardware, real-time

# skip VMAF for speed
python -m nebula.encoder input.mp4 --no-vmaf
```

Output JSON:
```json
{
  "output": "input_nebula.mp4",
  "vmaf": 95.44,
  "vmaf_p1": 93.07,
  "ratio": 0.191,
  "encoder": "x265",
  "proof_hash": "57b8f969f37c3a6d...",
  "encode_wall_s": 20.0,
  "cpu_pct_avg": 873.9,
  "cpu_pct_peak": 945.3,
  "p_cores": 4,
  "e_cores": 6
}
```

---

## What it does

### 1 · Content-aware encoding

`nebula/encoder.py` auto-detects what you're compressing and routes accordingly:

| Content | Auto-selected encoder | Why |
|---|---|---|
| Screen recordings, UI, text | x265 + screen preset | no-sao stops edge blur on text; tskip=1 gives 15-40% bitrate reduction on flat fills |
| Film / high-grain | x265 | AV1 film-grain synthesis fails above grain_level 0.5 (measured VMAF 54 on Jellyfish) |
| Clean natural video, animation | SVT-AV1 | Faster than x265 on long content (2.37× RT vs 0.25× RT), smaller at VMAF 95+ |

Zone-based CRF runs on top of codec selection: scene cuts get more bits, near-static zones fewer. Boundaries snap to keyframe intervals.

### 2 · Four encoder paths

```bash
--encoder x265          # HEVC — widest compatibility, best for text/grain
--encoder svt-av1       # AV1  — default for clean content, faster + smaller
--encoder vvc           # H.266/VVC — archival, ~25% smaller than HEVC at equal VMAF
--encoder videotoolbox  # Apple hardware HEVC — real-time draft/preview
```

### 3 · Apple Silicon optimised

On M-series Macs, x265 is tuned to match the P/E core split. Measured on M4 (4P+6E):

| Encoder | Wall time | CPU avg | Notes |
|---|---|---|---|
| x265 balanced | 20s | 874% | P-core frame threads, WPP fills E-cores |
| VideoToolbox | 1.6s | 134% | Encode in hardware, CPU = demux only |

### 4 · On-chain proof anchoring

`nebula/receipt.py` turns every encode into a tamper-evident proof:
- SHA-256 of the exact output bytes (always computed, never skipped)
- VMAF + ratio + timestamp commitment
- Receipt compressed with [Liquefy](https://github.com/Parad0x-Labs/liquefy)
- Archived to Arweave, anchored on Solana via `receipt_anchor` (`6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN`)

Anchoring is optional — bring your own keypair; without one it skips gracefully.

---

## Measured results

### Screen recording (4096×2304 60fps, 1.71 GB H.264 source)

| Mode | Output | Ratio | Quality |
|---|---|---|---|
| x265 CRF18, screen preset | **139 MB** | **11.7×** | SSIM 0.992, PSNR 47.3 dB |

### Jellyfish 1080p (hard benchmark, high motion + grain)

| Codec | Output | Ratio | VMAF | Time |
|---|---|---|---|---|
| x265 slow CRF22 +ref=8 | 5.8 MB | 5.2× | **95.44** | 264s |
| VVC QP30 | 2.4 MB | 12.4× | 91.46 | 2445s |

### From a 114 Mbps lossless master (production workflow)

| Codec | Output | Ratio | VMAF |
|---|---|---|---|
| **VVC QP34** | **14.2 MB** | **25.2×** | **95.81** |
| AV1 CRF32 | 4.17 MB | 32.6× | 94.21 |

### Full movie (BBB 692 MB, 10 min)

| Codec | Output | Ratio | VMAF | Time |
|---|---|---|---|---|
| AV1 p6 CRF32 | **40.7 MB** | **17×** | 94.77 | **252s (2.37× RT)** |
| x265 slow CRF23 | 218 MB | 3.2× | 96.04 | 2390s |

All measured on Apple M4, ffmpeg 8.1.1 arm64. Reproduce:

```bash
bash proof-pack/encode_jellyfish_safe.sh
```

---

## Mode reference

| Mode | x265 CRF | AV1 CRF | VVC QP | VTB quality | VMAF target |
|---|---|---|---|---|---|
| `safe` | 22 | 28 | 28 | 65 | ~96–97 |
| `balanced` | 23 | 32 | 32 | 55 | ~95–96 |
| `maximum` | 26 | 36 | 36 | 45 | ~90–94 |

---

## What it is — and isn't

- ✅ **Open (MIT)** — encoder, proof bridge, pipelines, verification tools. No sealed binaries.
- ✅ **Four codecs** — x265, SVT-AV1, VVC/H.266, VideoToolbox hardware path
- ✅ **Screen content preset** — sharpness-preserving flags for text/UI/screen recordings
- ✅ **Apple Silicon optimised** — P-core frame threads, WPP on E-cores, CPU metrics
- ✅ **On-chain proof optional** — local SHA-256 always computed; Solana anchor needs a keypair
- ❌ **Not a new codec** — smarter orchestration of existing ones
- ❌ **Not lossless** — perceptually close, not bit-identical
- ⏳ **Image/audio pipelines** — early stubs; video is the live path
- ⏳ **target-vmaf** — informational only; rate-control to hit a VMAF target is not yet implemented

---

## How this fits the Parad0x stack

| Layer | Repo | Does |
|---|---|---|
| 💸 Payments | [dna-x402](https://github.com/Parad0x-Labs/dna-x402) | x402 rail: quote → pay → verify → receipt → anchor |
| 🛠️ Build | [dna-x402-builders](https://github.com/Parad0x-Labs/dna-x402-builders) | Hosted kit: turn any API/bot into a paid agent |
| 🕶️ Privacy | [Dark-Null-Protocol](https://github.com/Parad0x-Labs/Dark-Null-Protocol) | Groth16 privacy settlement |
| 🗜️ Data | [liquefy](https://github.com/Parad0x-Labs/liquefy) | Columnar compression + audit trails |
| 🎬 Media | **nebula-media** (this repo) | Proof-carrying media compression |
| 🧠 Local AI | [nulla-local](https://github.com/Parad0x-Labs/nulla-local) | Local-first agent runtime |

**License:** MIT — © 2026 Parad0x Labs
