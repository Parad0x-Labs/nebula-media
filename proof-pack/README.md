# nebula-media proof-pack

Independent reproducibility kit for the nebula-media codec benchmarks.

---

## What this proves

| Clip            | Preset | Bitrate   | Output size | VMAF  | Speed   |
|-----------------|--------|-----------|-------------|-------|---------|
| Jellyfish 1080p | slow   | 1 866 kbps | 2.43 MB    | 88.1  | —       |
| Sintel 1080p    | slow   | —          | —           | 96.4  | 12.9x   |

Scores were produced with **libx265** and measured with **libvmaf v0.6.1**.
All source clips are freely licensed; see [sources/SOURCES.md](sources/SOURCES.md)
for download links and integrity notes.

---

## Prerequisites

- **ffmpeg** compiled with `--enable-libx265` and `--enable-libvmaf`
  (most package-manager builds include both)
- **python3** (for JSON parsing in the shell script; stdlib only)
- **bc** (for arithmetic in bash; ships with most Linux/macOS installs)
- ~4 GB free disk space for the source + encoded files

Check your ffmpeg build:

```
ffmpeg -codecs 2>/dev/null | grep -E "hevc|x265"
ffmpeg -filters 2>/dev/null | grep vmaf
```

Both lines must return output. If `libvmaf` is missing, build ffmpeg from source
with `--enable-libvmaf` or use a static build from https://johnvansickle.com/ffmpeg/.

---

## Step-by-step reproduction

### 1. Download source clips

Follow [sources/SOURCES.md](sources/SOURCES.md).

Default expected path for the Jellyfish clip:

```
G:\media compress\jellyfish_1080_10s.mp4
```

Override with the environment variable:

```
export JELLYFISH_PATH="/path/to/jellyfish_1080_10s.mp4"
```

### 2. Run the Jellyfish benchmark

```
bash proof-pack/encode_jellyfish_safe.sh
```

The script will:

1. Encode the clip with the exact x265 parameters used in the original benchmark.
2. Run libvmaf on the encoded output vs the original reference.
3. Write a JSON score file to `G:\media compress\proof_results\jellyfish_safe.json`.
4. Print a PASS/WARN result to the terminal.

### 3. Compare against expected results

Reference numbers are stored in [results/jellyfish_safe_expected.json](results/jellyfish_safe_expected.json).

Load both JSONs and compare `pooled_metrics.vmaf.mean`. A difference of **±0.5
VMAF points** is acceptable — this covers:

- Encoder micro-version differences (x265 3.4 vs 3.5, etc.)
- CPU SIMD path (AVX-512 vs AVX2)
- libvmaf model minor revisions
- OS scheduling / thread count variation

A difference larger than ±0.5 suggests a wrong source file, wrong encoder
preset, or a significantly different hardware path. Open an issue with your
`ffmpeg -version` and `x265 --version` output.

---

## What "reproducing" means

A benchmark is considered **reproduced** when:

- The same source file is used (verify SHA-256 if you want to be certain).
- The VMAF mean is within **±0.5** of the reference value.
- The encoded file size is within **±5%** of the reference (2.43 MB).

It is **not** required that:

- The encode time matches (depends heavily on CPU).
- The VMAF per-frame values are identical (x265 is deterministic on the same
  machine but not across different CPUs/OS/compiler).

---

## Exact ffmpeg encode command

For reference, the full command used to produce the reference encode:

```
ffmpeg -i jellyfish_1080_10s.mp4 \
  -c:v libx265 \
  -preset slow \
  -b:v 1866k \
  -pix_fmt yuv420p10le \
  -x265-params "aq-mode=3:aq-strength=1.0:rdoq-level=2:psy-rd=1.6:psy-rdoq=1.0:zones=0,30,b=1.400/30,90,b=1.120" \
  -an \
  jellyfish_safe_encoded.mp4
```

---

## File map

```
proof-pack/
  README.md                          — this file
  encode_jellyfish_safe.sh           — end-to-end encode + VMAF script
  sources/
    SOURCES.md                       — download URLs + integrity notes
  results/
    jellyfish_safe_expected.json     — reference VMAF JSON for comparison
```
