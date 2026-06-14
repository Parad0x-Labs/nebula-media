# Web0 encoding — universal vs platform targets

`nebula/web0.py` encodes images and video for **permanent Arweave storage** —
the quality/size sweet spot where every byte you don't upload is money saved
forever. It also produces **platform-compatible** outputs for places that won't
accept the efficient format.

> **⚠️ `--target x` is NOT the efficient output — it's a compatibility downgrade.**
> It's bigger and lower quality than `universal` (H.264 1080p vs AV1, capped at
> 1080p). **Only use it when you need to post to X/Twitter.** For storage,
> sharing, `.null` publishing, or archiving, always use `universal` (the default).
> Measured on the same source: universal AV1 = 22 MB / VMAF 99.9; X = 27 MB / VMAF 95.2.

## Two targets

```bash
python -m nebula.web0 input.mp4                 # universal (default)
python -m nebula.web0 input.mp4 --target x      # Twitter/X-compatible
```

| | `universal` (default) | `x` |
|---|---|---|
| **Use for** | Arweave / storage / archival | Uploading to Twitter/X |
| **Video codec** | AV1 (or x265 for grain/screen) | H.264 High |
| **Image format** | AVIF | WebP |
| **Resolution** | full (untouched) | downscaled to ≤1080p |
| **Audio** | preserved | AAC (silent track synthesised if none) |
| **Quality target** | VMAF 93–95, smallest file | clean 1080p for X's own re-encode |
| **Why** | best quality-per-byte, paid once forever | X rejects AV1/AVIF, caps at 1080p |

**Universal is far superior** — smaller files at higher quality. Use it for
everything *except* when a platform won't take it. The `x` target is a
deliberate downgrade for compatibility, not the encode you'd archive.

## Why two are needed

A 4K AV1 file is the right thing to store on Arweave: full resolution, tiny,
VMAF 99. But you can't upload it to X — **X rejects AV1, caps at 1080p, and
re-encodes everything to ~2 Mbps anyway**. So `--target x` produces the
upload-safe H.264 1080p version. Same recording, two outputs:

```bash
python -m nebula.web0 demo.mov                # demo_web0.mp4  — 22 MB AV1 4K  → Arweave
python -m nebula.web0 demo.mov --target x     # demo_X.mp4     — 27 MB H264 1080p → X
```

Measured on a real 2m38s 4K screen recording (538 MB source, Apple M4):

| Output | Codec | Res | Size | VMAF | Arweave cost |
|---|---|---|---|---|---|
| universal | AV1 | 4096×2178 | **22 MB** | 99.9 | ~$0.41 |
| x | H.264 | 1920×1020 | 27 MB | 95.2 | ~$0.49 |

Note the X file is **bigger and lower quality** despite being lower resolution —
that's the cost of H.264 vs AV1. It exists only because X won't take the good one.

## X (Twitter) upload limits

Worth knowing before you post:

- **Duration:** free accounts cap at **2:20 (140s)**. Longer needs X Premium —
  no compression trick gets around this. `--target x` logs a warning past 140s.
- **Resolution:** X displays max 1080p and re-encodes anything larger.
- **Size:** 512 MB (free), GBs (Premium).
- **Codec:** H.264 only for most accounts; AV1/HEVC rejected.

## Arweave cost model

```python
from nebula.web0 import estimate_arweave_cost
estimate_arweave_cost(22 * 1048576)   # → {'ar': 0.000826, 'usd': 0.0248, ...}
```

Cost scales linearly with bytes, and Arweave is **pay-once-store-forever** — so
the universal target's smaller files mean permanent savings on every asset.
Pass live pricing with `--ar-per-gb` and `--ar-usd` (defaults ~0.628 AR/GB at
$30/AR). Figures are estimates — AR price floats.

```bash
python -m nebula.web0 *.png --ar-usd 42          # batch, custom AR price
```

## Images

- `universal` → **AVIF** (50–90% smaller than JPEG/PNG at equal quality)
- `x` → **WebP** (X accepts WebP; it does **not** accept AVIF)

Screenshots/text get higher quality (sharper glyphs); photos compress harder.
Content type is auto-detected, or pass `--quality` / `--target-ssim` to override.
See [overview.md](overview.md) for the full image + `.null` page pipeline.
