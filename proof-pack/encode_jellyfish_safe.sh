#!/usr/bin/env bash
# encode_jellyfish_safe.sh
# Reproduces the Jellyfish 1080p 10s benchmark from the nebula-media proof-pack.
#
# Requirements:
#   ffmpeg built with --enable-libx265 and --enable-libvmaf
#   vmaf models accessible to ffmpeg (usually in /usr/share/vmaf or via VMAF_MODEL env)
#
# Usage:
#   ./encode_jellyfish_safe.sh
#   JELLYFISH_PATH=/my/path/jellyfish.mp4 ./encode_jellyfish_safe.sh
#
# Output:
#   Encoded file : G:\media compress\jellyfish_safe_encoded.mp4
#   VMAF JSON    : G:\media compress\proof_results\jellyfish_safe.json

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INPUT="${JELLYFISH_PATH:-G:\\media compress\\jellyfish_1080_10s.mp4}"
OUTPUT_DIR="G:\\media compress"
RESULTS_DIR="${OUTPUT_DIR}\\proof_results"
ENCODED="${OUTPUT_DIR}\\jellyfish_safe_encoded.mp4"
VMAF_JSON="${RESULTS_DIR}\\jellyfish_safe.json"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [ ! -f "${INPUT}" ]; then
  echo "ERROR: Input file not found: ${INPUT}"
  echo "  Set the JELLYFISH_PATH environment variable or place the file at the default path."
  echo "  Download: curl -L -o \"${INPUT}\" https://files.catbox.moe/d5r7i6.mp4"
  exit 1
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "ERROR: ffmpeg not found in PATH"; exit 1; }

mkdir -p "${RESULTS_DIR}"

# ---------------------------------------------------------------------------
# Step 1 — Encode
# ---------------------------------------------------------------------------
echo "=== Step 1: Encoding with libx265 (preset slow, 1866 kbps) ==="
echo "    Input  : ${INPUT}"
echo "    Output : ${ENCODED}"
echo ""

ffmpeg -y \
  -i "${INPUT}" \
  -c:v libx265 \
  -preset slow \
  -b:v 1866k \
  -pix_fmt yuv420p10le \
  -x265-params \
    "aq-mode=3:\
aq-strength=1.0:\
rdoq-level=2:\
psy-rd=1.6:\
psy-rdoq=1.0:\
zones=0,30,b=1.400/30,90,b=1.120" \
  -an \
  "${ENCODED}"

ENCODED_SIZE_BYTES=$(wc -c < "${ENCODED}" | tr -d ' ')
ENCODED_SIZE_MB=$(echo "scale=2; ${ENCODED_SIZE_BYTES} / 1048576" | bc)
echo ""
echo "    Encoded size: ${ENCODED_SIZE_MB} MB  (expected ~2.43 MB)"

# ---------------------------------------------------------------------------
# Step 2 — VMAF scoring
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 2: Measuring VMAF ==="
echo "    Comparing ${ENCODED} vs reference ${INPUT}"
echo "    Output JSON: ${VMAF_JSON}"
echo ""

# libvmaf filter: scale encoded to 1920x1080 if needed, compare against ref
# [0:v] = distorted (encoded), [1:v] = reference (original)
ffmpeg -y \
  -i "${ENCODED}" \
  -i "${INPUT}" \
  -lavfi \
    "[0:v]scale=1920:1080:flags=bicubic[distorted];\
[1:v]scale=1920:1080:flags=bicubic[ref];\
[distorted][ref]libvmaf=log_path='${VMAF_JSON}':log_fmt=json:model=version=vmaf_v0.6.1:n_threads=4" \
  -f null - 2>&1 | grep -E "(VMAF score|frame=|speed=)" || true

# ---------------------------------------------------------------------------
# Step 3 — Print result
# ---------------------------------------------------------------------------
echo ""
echo "=== Results ==="

if [ -f "${VMAF_JSON}" ]; then
  # Extract pooled mean VMAF from JSON using python (widely available)
  VMAF_SCORE=$(python3 -c "
import json, sys
with open('${VMAF_JSON}') as f:
    d = json.load(f)
# Support both old and new vmaf JSON schemas
try:
    score = d['pooled_metrics']['vmaf']['mean']
except KeyError:
    score = d['VMAF score']
print(f'{score:.1f}')
" 2>/dev/null || echo "parse-error")

  echo "    VMAF score : ${VMAF_SCORE}  (expected 88.1, tolerance ±0.5)"
  echo "    Output JSON: ${VMAF_JSON}"
  echo ""

  if python3 -c "
score = float('${VMAF_SCORE}')
assert 87.6 <= score <= 88.6, f'VMAF {score} outside tolerance'
" 2>/dev/null; then
    echo "    PASS — score within ±0.5 of reference 88.1"
  else
    echo "    WARN — score outside ±0.5 window. Check encoder version, model, and source file."
  fi
else
  echo "    WARN: VMAF JSON not found at ${VMAF_JSON}. Check ffmpeg libvmaf build."
fi

echo ""
echo "Done."
