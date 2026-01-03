#!/usr/bin/env bash
# Media Verification Tool
# Purpose: duration, resolution, and checksum verification.

FILE="$1"
if [ -z "$FILE" ]; then
  echo "Usage: verify_media.sh <file>"
  exit 1
fi

echo "Verifying media integrity for: $FILE"
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FILE"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$FILE"
sha256sum "$FILE"

