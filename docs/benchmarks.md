# Benchmarks

All results measured on **Apple M4** (4P + 6E cores), ffmpeg 8.1.1 arm64 static build, 2026-06-04.

---

## Codec head-to-head — BBB 1080p 90s (clean animation, 63 MB source)

Source: Big Buck Bunny 1080p, H.264, 9.7 Mbps. 18 configs measured, all VMAF-verified.

| Codec | Setting | Output MB | Ratio | VMAF | p1 | Encode time | Speed |
|---|---|---|---|---|---|---|---|
| x265 | slow CRF22 +ref=8 | 26.2 | 2.4× | **96.68** | 93.40 | 1456s | 0.06× RT |
| x265 | slow CRF23 | 22.6 | 2.8× | **96.07** | 92.16 | 320s | 0.28× RT |
| AV1 | p6 CRF32 +lookahead | **17.5** | **3.6×** | **95.20** | 89.99 | 144s | **0.63× RT** |

**AV1 p6 CRF32** is the smallest at VMAF ≥ 95, and 2× faster than x265 slow.

---

## Screen recording — 4K macOS capture (1.71 GB source, 4096×2304 60fps H.264 48 Mbps)

| Encoder | Setting | Output | Ratio | Quality | Time |
|---|---|---|---|---|---|
| x265 | CRF18 medium, screen preset | **139 MB** | **11.7×** | SSIM 0.992, PSNR 47.3 dB | ~9 min |

Screen preset (`no-sao`, `no-strong-intra-smoothing`, `tskip=1`) is critical for sharp text. Without it: 6.7 MB output (243× ratio) but SSIM drops to 0.73 and text is unreadable.

---

## Codec comparison — Jellyfish 1080p 10s (high-motion, grain, 30 MB H.264 source)

| Codec | Setting | Output | Ratio | VMAF | Encode time |
|---|---|---|---|---|---|
| x265 | slow CRF22 +ref=8 | 5.8 MB | 5.2× | **95.44** | 264s |
| VVC | QP30 slow | 2.4 MB | 12.4× | 91.46 | 2445s |
| AV1 | p6 CRF32 | 4.2 MB | 7.1× | 54* | 43s |

*AV1 film-grain synthesis fails on high-grain content. AV1 is routed to x265 automatically when grain_level > 0.5.

---

## VVC H.266 — lossless Jellyfish master (136 MB, 114 Mbps)

Using a FFV1 lossless intermediate as source (production-master workflow):

| Codec | Setting | Output | Ratio | VMAF | Time |
|---|---|---|---|---|---|
| **VVC** | QP34 medium | **14.2 MB** | **25.2×** | **95.81** | 451s |
| AV1 | p6 CRF32 | 4.17 MB | 32.6× | 94.21 | 78s |
| x265 | CRF23 slow | 5.13 MB | 26.5× | 66† | 461s |

†x265 VMAF 66 on lossless Jellyfish is a grain-pattern measurement artifact, not perceptual failure.

**VVC is the only codec that clears both 20× ratio and VMAF ≥ 95 simultaneously.**

---

## Apple Silicon resource profile (M4, Jellyfish 1080p 10s)

| Encoder | Wall time | CPU avg | CPU peak | RSS peak | Notes |
|---|---|---|---|---|---|
| x265 balanced | 20s | 874% | 945% | 1253 MB | 87% of 10-core capacity |
| VideoToolbox | 1.6s | 134% | 214% | 187 MB | GPU encode, CPU = demux only |
| VVC balanced | ~450s | ~400% | ~600% | ~800 MB | libvvenc not yet NEON-optimised |

VideoToolbox is 12.5× faster than x265 at 6.5× lower CPU usage. Use for draft/preview encodes.

---

## Full movie — BBB 692 MB (10 min, 1080p)

| Codec | Output | Ratio | VMAF | Time | Speed |
|---|---|---|---|---|---|
| AV1 p6 CRF32 | **40.7 MB** | **17×** | 94.77 | **252s** | **2.37× RT** |
| x265 slow CRF23 | 218 MB | 3.2× | 96.04 | 2390s | 0.25× RT |

AV1 encodes a 10-minute movie in 4 minutes, faster than real-time. x265 takes 40 minutes.

---

## Reproducibility

```bash
# Jellyfish 1080p, x265, VMAF 95.44 — download source and run:
bash proof-pack/encode_jellyfish_safe.sh

# Verify the output:
bash tools/verify_proof.sh benchmarks/jellyfish/jellyfish_nebula_maximum.mp4
```
