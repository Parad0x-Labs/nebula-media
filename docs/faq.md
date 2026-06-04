# FAQ

### What encoders does nebula support?

Four:
- **x265** (HEVC/H.265) — widest device compatibility, best for grain/screen content. Default for screen recordings and high-grain film.
- **svt-av1** (AV1) — default for clean natural video. Faster than x265 on long content, smaller at VMAF 95+.
- **vvc** (H.266/VVC) — next-gen via libvvenc. ~25% smaller than HEVC at equal VMAF. Archival use — libvvenc is not yet NEON-optimised on Apple Silicon so it runs ~10-20× slower than x265.
- **videotoolbox** — Apple hardware HEVC. Real-time on M-series. Use for draft previews; quality ceiling is lower than libx265.

Auto-selection (`--encoder` omitted): screen content → x265 screen preset, grain > 0.5 → x265, everything else → svt-av1.

### What do the modes mean?

| Mode | x265 CRF | AV1 CRF | VVC QP | VMAF target |
|---|---|---|---|---|
| `safe` | 22 | 28 | 28 | ~96–97 |
| `balanced` | 23 | 32 | 32 | ~95–96 |
| `maximum` | 26 | 36 | 36 | ~90–94 |

CRFs are calibrated from measured benchmarks on BBB 1080p. Actual VMAF depends on source content.

### What does the "screen content" preset do?

Screen recordings, UI, and text content need different settings than natural video. Nebula auto-detects screen content (low grain, high resolution, H.264/HEVC source codec) and applies:
- `no-sao` — SAO filter blurs text edges; disabling it is the single biggest sharpness win
- `no-strong-intra-smoothing` — prevents 32×32 block smear on flat fills
- `tskip=1` — DCT-skip for near-flat 4×4 blocks; 15-40% bitrate reduction on text at equal quality
- `aq-mode=4`, `aq-strength=0.6` — variance-based AQ, lighter for large flat regions

Without the screen preset, a 1.71 GB screen recording compresses to 6.7 MB (243× ratio) but text is unreadable. With it: 139 MB (11.7× ratio), text stays sharp.

### Can it achieve 20× compression?

Depends entirely on the source. **Yes** from high-bitrate masters. **No** from already-compressed distribution content.

- macOS screen recording (48 Mbps H.264) → x265 CRF18: **11.7×** at visually lossless
- Jellyfish lossless master (114 Mbps FFV1) → VVC QP34: **25.2× at VMAF 95.81**
- BBB 1080p full movie (9.7 Mbps H.264) → AV1 CRF32: **17×** at VMAF 94.77
- BBB 1080p full movie (9.7 Mbps H.264) → x265 CRF23: **3.2×** at VMAF 96

Compression ratio = source_bitrate / output_bitrate. If the source is already at near-optimal quality, there is no fat to cut.

### Is VVC actually better than HEVC?

Yes, measurably — but only at equal VMAF, not equal CRF. At VMAF 95 on a 114 Mbps lossless source, VVC QP34 (14.2 MB) vs x265 CRF22 (5.8 MB at VMAF 67 — grain measurement artifact on that source). The real comparison requires finding the x265 CRF that also hits VMAF 95 and comparing file sizes, which we haven't done on identical content yet. Community benchmarks suggest 25-40% bitrate savings for VVC at equal VMAF on natural video. Encode speed on Apple Silicon arm64 is the current problem — libvvenc isn't NEON-optimised.

### What's in the CPU metrics output?

Every encode now reports:
- `encode_wall_s` — clock seconds for the encode pass
- `cpu_pct_avg` — average CPU utilisation across all cores (e.g. 874% on 10-core M4 = 87.4% of total)
- `cpu_pct_peak` — peak 1-second sample
- `rss_peak_mb` — peak resident memory
- `p_cores` / `e_cores` — detected Apple Silicon core split

These let you understand whether the encoder is saturating the machine or leaving headroom for other tasks.

### What is the proof / receipt?

Each encode produces a SHA-256 digest of the exact output file. With a Solana keypair, nebula can also:
1. Build a 32-byte commitment: `SHA-256(output) + VMAF + ratio + timestamp`
2. Compress the receipt with Liquefy
3. Archive to Arweave
4. Anchor the commitment on Solana mainnet via `receipt_anchor`

Without a keypair, it skips anchoring and still gives you the local SHA-256 for manual verification.

### Is this production-ready?

**Alpha.** The encoder pipeline is functional and benchmarked. Known rough edges:
- VVC is slow on Apple Silicon (libvvenc arm64 not yet optimised)
- VideoToolbox writes `hev1` tag — opens in QuickTime but Finder thumbnail may not render
- Image and audio pipelines are stubs
- `--target-vmaf` is still informational — it logs a warning if quality misses but doesn't rate-control to hit the target
