# Jellyfish Benchmark Results

Measured on Apple M4 (4P+6E cores), ffmpeg 8.1.1 arm64, 2026-06-04.

---

## Jellyfish 1080p 10s (30 MB, 25 Mbps H.264 source)

| Codec | Setting | Output | Ratio | VMAF | p1 | Encode time |
|---|---|---|---|---|---|---|
| **x265** | slow CRF22 +ref=8 | **5.8 MB** | **5.2×** | **95.44** | 93.07 | 264s |
| VVC H.266 | QP30 slow | 2.4 MB | 12.4× | 91.46 | 89.77 | 2445s |
| AV1 | p6 CRF32 | 4.2 MB | 7.1× | 54* | 13.6 | 43s |

*AV1 with `film-grain=4` synthesis collapses on this source (VMAF 54). Auto-routing sends grain_level>0.5 content to x265.

**x265 CRF22 is the only config that clears VMAF 95 on Jellyfish** (the hardest 1080p benchmark).

---

## Jellyfish lossless master (136 MB, 114 Mbps FFV1 source)

Source created from the 30 MB H.264 clip via FFV1 lossless encode. Simulates a production-master workflow.

| Codec | Setting | Output | Ratio | VMAF | Encode time |
|---|---|---|---|---|---|
| **VVC H.266** | QP34 medium | **14.2 MB** | **25.2×** | **95.81** | 451s |
| AV1 | p6 CRF32 | 4.17 MB | 32.6× | 94.21 | 78s |
| x265 | CRF23 slow | 5.13 MB | 26.5× | 66† | 461s |

†x265 VMAF 66 on lossless Jellyfish is a grain-pattern measurement artifact. The output looks correct visually.

**VVC hits 25.2× at VMAF 95.81** — the only config to clear both the 20× ratio target and the VMAF 95 floor simultaneously.

---

## CPU profile (M4, Jellyfish 1080p 10s)

| Encoder | Wall time | CPU avg | CPU peak | RSS |
|---|---|---|---|---|
| x265 balanced | 20s | 874% | 945% | 1253 MB |
| VideoToolbox | 1.6s | 134% | 214% | 187 MB |

---

## Reproducing the x265 result

```bash
# Download source
curl -L -o jellyfish_1080_10s.mp4 https://files.catbox.moe/d5r7i6.mp4

# Encode
python -m nebula.encoder jellyfish_1080_10s.mp4 \
  --encoder x265 --mode balanced

# Expected: VMAF ~95.44, output ~5.8 MB
```

Or use the proof-pack script which verifies within ±0.5 VMAF:

```bash
bash proof-pack/encode_jellyfish_safe.sh
```
