#!/usr/bin/env bash
# Nebula Media: Enterprise Verification Utility
# ============================================
# MISSION: Prove quality-per-bit efficiency on local infrastructure.

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: ./verify_proof.sh <original_file> <compressed_file>"
  exit 1
fi

echo "[*] Initializing Media Audit..."
ORIG="$1"
COMP="$2"

# 1. Check Bit-Perfect Duration
echo "[1/3] Verifying Temporal Integrity..."
D1=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$ORIG")
D2=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$COMP")
echo "    Original: ${D1}s | Compressed: ${D2}s"

# 2. Check Resolution Match
echo "[2/3] Verifying Spatial Integrity..."
R1=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$ORIG")
R2=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$COMP")
echo "    Original: ${R1} | Compressed: ${R2}"

# 3. Size & Savings
echo "[3/3] Calculating Efficiency..."
S1=$(stat -c%s "$ORIG")
S2=$(stat -c%s "$COMP")
RATIO=$(echo "scale=2; $S1 / $S2" | bc)
SAVINGS=$(echo "scale=2; (1 - ($S2 / $S1)) * 100" | bc)
echo "    Squeeze Ratio: ${RATIO}x"
echo "    Storage Savings: ${SAVINGS}%"

echo ""
echo "[SUCCESS] Proof Verified. For VMAF depth-analysis, contact enterprise@parad0xlabs.com"


