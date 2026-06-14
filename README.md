# nebula-media 🎬

**Proof-carrying media compression.** Scene-aware video across four codecs, AVIF images, and a one-command **.null page mode** — with a cryptographic receipt that proves exactly what quality you shipped, anchored on Solana.

![status: alpha](https://img.shields.io/badge/status-alpha-orange) ![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![codecs: x265·AV1·VVC·VideoToolbox·AVIF](https://img.shields.io/badge/codecs-x265%20·%20AV1%20·%20VVC%20·%20VideoToolbox%20·%20AVIF-14F195) ![proof: SHA--256·VMAF·SSIM·Solana](https://img.shields.io/badge/proof-SHA--256%20·%20VMAF%20·%20SSIM%20·%20Solana-36e0ff)

---

## Quick start

```bash
pip install -e .          # image + page modes are pure Python (Pillow/libavif)

# VIDEO — balanced mode: auto-selects codec, measures VMAF, outputs proof hash
python -m nebula.encoder input.mp4
python -m nebula.encoder input.mp4 --encoder vvc --mode safe
python -m nebula.encoder input.mp4 --no-vmaf            # skip VMAF for speed

# IMAGES — AVIF with SSIM measurement + Arweave cost estimate (no ffmpeg needed)
python -m nebula.web0 photo.jpg
python -m nebula.web0 logo.png --target-ssim 0.97

# PLATFORM TARGET — universal (AV1/AVIF, for Arweave) vs X (H.264 1080p, Twitter-safe)
python -m nebula.web0 clip.mp4                # universal: best quality+size, store on Arweave
python -m nebula.web0 clip.mp4 --target x     # X-compatible: H.264 1080p + audio (X rejects AV1)

# .NULL PAGE — compress a whole site folder, rewrite refs, emit proof manifest
python -m nebula.page ./my-site                          # → ./my-site_web0
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

### 3 · Images → AVIF, pure Python

`nebula/web0.py` re-encodes images for the web — built for Arweave, where every
byte is paid for permanently. No ffmpeg: Pillow ≥ 11.3 ships libavif.

- **Alpha preserved** (transparent logos stay transparent), **EXIF orientation applied**
- **SSIM measured** on every encode (own scikit-image-compatible implementation), with an
  automatic retry at higher quality if it lands below the content-type floor
- **Never grows a file** — if AVIF isn't smaller, the original bytes are kept
- Content-type tiers: photo q80 · graphic q85 · screenshot/text q88 (glyphs get more bits)
- Animated GIFs are refused, not silently flattened to frame 1

### 4 · .null / Web0 page mode

`nebula/page.py` is the pre-publish step for a .null site: copy the folder,
convert every image, **rewrite the references** in HTML/CSS/JS (root-absolute
and document-relative forms), and write a manifest with per-file SHA-256 proof
hashes + the estimated Arweave cost. Output is always a working page — anything
that can't be made smaller ships byte-for-byte.

### 5 · Apple Silicon optimised

On M-series Macs, x265 is tuned to match the P/E core split. Measured on M4 (4P+6E):

| Encoder | Wall time | CPU avg | Notes |
|---|---|---|---|
| x265 balanced | 20s | 874% | P-core frame threads, WPP fills E-cores |
| VideoToolbox | 1.6s | 134% | Encode in hardware, CPU = demux only |

### 6 · On-chain proof anchoring

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

### Images — Kodak test set (AVIF via Pillow/libavif)

| Image | Setting | Output | Ratio | SSIM |
|---|---|---|---|---|
| kodim23 parrots (545 KB PNG) | photo q80 | **47.7 KB** | **11.4×** | 0.9747 |
| kodim23 | screenshot q88 | 74.9 KB | 7.3× | 0.9843 |
| kodim13 river detail (803 KB PNG) | photo q80 | **142.4 KB** | **5.6×** | 0.9859 |
| kodim13 | screenshot q88 | 184.4 KB | 4.4× | 0.9948 |

kodim13 is the hardest image in the set — the 5.6–11.4× spread is the honest
range for real photos. Flat graphics compress far more; don't quote these as
universal.

Video measured on Apple M4 (ffmpeg 8.1.1 arm64); images on Windows 10 x64
(Pillow 12.2.0). Reproduce both:

```bash
bash proof-pack/encode_jellyfish_safe.sh      # video — needs ffmpeg+libvmaf
bash proof-pack/encode_kodak_images.sh        # images — pure Python, auto-downloads sources
```

---

## Publish a .null page (Web0)

Arweave is pay-once, store-forever — compression isn't cosmetics, it's the
publishing economics. The flow with the [web0](https://github.com/Parad0x-Labs/web0)
stack:

```bash
# 1. compress the site folder (images → AVIF, refs rewritten, manifest written)
python -m nebula.page ./my-site
# → my-site_web0/  + nebula_page_manifest.json (per-file SHA-256 + SSIM + cost)

# 2. check it locally — open my-site_web0/index.html in a browser

# 3. publish with web0's one-command publisher (uploads via Irys, re-points
#    your .null domain on Solana, verifies resolution)
node scripts/publish.mjs ./my-site_web0/index.html --name yourname
```

The JSON summary tells you what you'll pay before you upload — e.g. a 3 MB
image-heavy page typically lands well under 1 MB, and the manifest is the
receipt: every published file's hash, quality score, and size, ready to anchor
through `receipt_anchor` like any other nebula proof.

AVIF renders in every current browser (Chrome 85+, Firefox 93+, Safari 16.4+);
the .null resolver extension is Chromium-based, so published pages decode
everywhere they can be viewed.

---

## Mode reference

| Mode | x265 CRF | AV1 CRF | VVC QP | VTB quality | VMAF target |
|---|---|---|---|---|---|
| `safe` | 22 | 28 | 28 | 65 | ~96–97 |
| `balanced` | 23 | 32 | 32 | 55 | ~95–96 |
| `maximum` | 26 | 36 | 36 | 45 | ~90–94 |

Image tiers (auto-detected, override with `--content-type` / `--quality`):

| Content type | AVIF q | WebP q | SSIM floor (auto-retry below) |
|---|---|---|---|
| photo | 80 | 82 | 0.92 |
| graphic | 85 | 88 | 0.94 |
| screenshot / text | 88 | 92 | 0.96 |

---

## What it is — and isn't

- ✅ **Open (MIT)** — encoder, proof bridge, pipelines, verification tools. No sealed binaries.
- ✅ **Four video codecs** — x265, SVT-AV1, VVC/H.266, VideoToolbox hardware path
- ✅ **Image/AVIF pipeline** — pure Python (Pillow/libavif), alpha-safe, SSIM-measured, never grows a file
- ✅ **.null page mode** — site folder in, upload-ready folder + proof manifest out
- ✅ **Screen content preset** — sharpness-preserving flags for text/UI/screen recordings
- ✅ **Apple Silicon optimised** — P-core frame threads, WPP on E-cores, CPU metrics
- ✅ **On-chain proof optional** — local SHA-256 always computed; Solana anchor needs a keypair
- ❌ **Not a new codec** — smarter orchestration of existing ones
- ❌ **Not lossless** — perceptually close, not bit-identical
- ⏳ **Audio pipeline** — early stub
- ⏳ **Animated GIF → video** — not automated yet; the image path refuses animations rather than dropping frames
- ⏳ **target-vmaf** — informational only; rate-control to hit a VMAF target is not yet implemented

---

## How this fits the Parad0x stack

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

**License:** MIT — © 2026 Parad0x Labs
