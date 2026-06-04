# Benchmark Methodology

## Hardware

All benchmarks on this repo run on **Apple M4** (4 Performance + 6 Efficiency cores, arm64, macOS 26.3) using ffmpeg 8.1.1 arm64 static build. Results are reproducible on any machine with the same ffmpeg build — speeds will differ, quality (VMAF) numbers are content-deterministic.

## Source clips

| Clip | Resolution | FPS | Codec | Bitrate | Why |
|---|---|---|---|---|---|
| Jellyfish 1080p 10s | 1920×1080 | 30 | H.264 | 25 Mbps | Hard benchmark: high motion, dense grain, no temporal redundancy |
| Big Buck Bunny 1080p full | 1920×1080 | 24 | H.264 | 9.7 Mbps | Typical compressed distribution content |
| Jellyfish FFV1 lossless | 1920×1080 | 30 | FFV1 | 114 Mbps | Production-master workflow: measures codec ceiling from uncompressed source |
| macOS screen recording | 4096×2304 | 60 | H.264 | 48 Mbps | Real-world screen content: text, UI, motion |

## Quality metrics

**VMAF (Video Multi-Method Assessment Fusion)** is the primary metric. Model selection:
- `vmaf_v0.6.1` for HD content (≤ 1080p)
- `vmaf_4k_v0.6.1` for UHD/4K (auto-selected when width ≥ 3840 or height ≥ 2160)

VMAF is measured as **mean** and **1st-percentile** (p1). The mean catches average quality; p1 catches quality cliffs — the worst ~1% of frames. A high mean with a low p1 means there are bad frames hiding behind a good average.

**SSIM and PSNR** are supplementary metrics used for screen/text content where VMAF over-scores (VMAF was trained on natural video and doesn't penalise grain-pattern changes the same way it penalises texture loss on text).

**VMAF on grain content** has a known limitation: when the encoder changes the grain pattern (even at high perceptual quality), VMAF scores it as quality loss. The Jellyfish x265 VMAF 66 result on a lossless source is a measurement artifact, not perceptual failure — the visual output is sharp. SSIM and eyeball review are the correct check on grain-heavy content.

## Speed reporting

- **Wall time**: clock time for the encode pass only (excludes VMAF measurement)
- **Speed multiplier (x RT)**: `source_duration / wall_time`. 1.0× = real-time; 2.37× = faster than real-time.
- **CPU avg/peak**: psutil sampling every 500 ms across all cores. 874% on a 10-core machine = 87.4% total CPU consumed.

## Encode settings

All benchmark encodes use nebula's default pipeline unless explicitly noted:
- Scene detection: ffmpeg `scdet` filter, threshold 0.35
- Zone snapping: keyframe-boundary alignment (keyint=250)
- x265 Apple Silicon tuning: `frame-threads=4` (P-cores), `pools=+` (all cores for WPP)
- VMAF subsampling: `n_subsample=6` (every 6th frame), accuracy within ±0.01 vs full measurement
- VMAF threads: `min(cpu_count, 8)`

## Disclaimers

Results are content-dependent. Jellyfish is the hardest 1080p benchmark (high motion, maximum grain). Clean animation, talking-head, and screen recordings compress significantly more. Compression ratios depend on source bitrate — re-encoding already-compressed content at 9.7 Mbps will not give the same ratios as encoding from a 114 Mbps lossless master.
