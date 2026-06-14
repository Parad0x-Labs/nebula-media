# AGENTS.md — run nebula-media locally

For AI coding agents (Claude Code, Codex, Cursor, etc.) and humans setting this
up cold. nebula-media is a media-compression CLI: scene-aware video (x265 / AV1 /
VVC / Apple VideoToolbox), AVIF images, and a `.null` static-site packer — every
encode carries a SHA-256 + quality (VMAF/SSIM) proof. MIT.

## Setup (run this top to bottom)

```bash
git clone https://github.com/Parad0x-Labs/nebula-media.git
cd nebula-media

# 1. ffmpeg — REQUIRED for video (image + page modes are pure Python, no ffmpeg).
#    macOS:          brew install ffmpeg
#    Ubuntu/Debian:  sudo apt-get update && sudo apt-get install -y ffmpeg
#    Fedora:         sudo dnf install -y ffmpeg
#    Windows:        winget install Gyan.FFmpeg   (or https://ffmpeg.org/download.html)

# 2. Python 3.10+ deps. Use a venv to avoid PEP-668 "externally-managed" errors:
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. Verify the whole setup (deps + ffmpeg + codecs + a real smoke encode):
bash scripts/check.sh
```

If `scripts/check.sh` prints **READY**, you're done. It tells you exactly what's
missing otherwise (e.g. which ffmpeg codec is absent and how to install it).

## Run it

```bash
# VIDEO — auto-picks codec, measures VMAF, prints a proof hash
python -m nebula.encoder input.mp4

# WEB0 — two targets:
python -m nebula.web0 input.mp4               # universal: AV1/AVIF, smallest file (for Arweave/storage)
python -m nebula.web0 input.mp4 --target x    # Twitter/X-compatible: H.264 1080p + AAC, upload-ready

# IMAGES — AVIF, no ffmpeg needed
python -m nebula.web0 photo.jpg

# .NULL PAGE — compress a site folder, rewrite refs, write a proof manifest
python -m nebula.page ./my-site
```

Installed console scripts (after `pip install -e .`): `nebula`, `nebula-web0`, `nebula-page`.

## Common tasks (exact commands)

| Goal | Command |
|---|---|
| Shrink a video for storage | `python -m nebula.web0 FILE` (AV1, smallest) |
| Make a video uploadable to X | `python -m nebula.web0 FILE --target x` |
| Shrink images for the web | `python -m nebula.web0 IMG.png` (→ AVIF) |
| Force a codec | `python -m nebula.encoder FILE --encoder x265\|svt-av1\|vvc\|videotoolbox` |
| Faster (skip quality metric) | add `--no-vmaf` (encoder) or `--no-quality` (web0) |
| Pick a mode | `--mode safe\|balanced\|maximum` |

## Requirements & gotchas

- **Python ≥ 3.10.** Deps (`pip install -e .`): numpy, scipy, pillow≥11.3 (AVIF), psutil.
- **Video needs ffmpeg + ffprobe on PATH. Image/page modes do NOT.**
- **Codec availability depends on your ffmpeg build** (`scripts/check.sh` reports it):
  - H.264 (`--target x`) → `libx264` — in essentially every ffmpeg
  - HEVC → `libx265` · AV1 → `libsvtav1` · VVC → `libvvenc` (rare, optional)
  - Apple hardware HEVC → `hevc_videotoolbox` (macOS only)
  - VMAF scores → `libvmaf`; if absent, encodes still run and VMAF returns -1
- **X upload limits** (the tool fixes format, not these): H.264 ≤1080p + AAC; free
  accounts cap at **2:20** — 30-min videos need **X Premium**. `--target x` warns past 2:20.
- **This repo ships no ffmpeg binaries** — install your own (step 1).
- On Apple Silicon, libvvenc (VVC) runs ~10–20× slower than x265 (not yet NEON-optimised).

## File map

| Path | What |
|---|---|
| `nebula/encoder.py` | video pipeline — codec routing, scene zones, VMAF, proof hash |
| `nebula/web0.py` | Arweave / X image + video targets (`encode_for_web0`, `encode_for_x`) |
| `nebula/page.py` | `.null` static-site packer |
| `nebula/screen_codec.py` | layered screen-recording codec (experimental) |
| `nebula/metrics.py` | SSIM / PSNR / VMAF (pure numpy) |
| `nebula/quality_commitment.py` | Merkle quality commitment (proof layer; ZK-extensible) |
| `nebula/receipt.py` | Solana + Arweave anchoring (optional, needs a keypair) |
| `docs/` | overview · benchmarks · methodology · faq · web0 |
| `scripts/check.sh` | setup verifier (deps + ffmpeg + codecs + smoke test) |
