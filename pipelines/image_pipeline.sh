#!/usr/bin/env bash
# Nebula Web0 image pipeline — AVIF (or WebP) encode with SSIM measurement,
# alpha preservation, and a never-grow guard.  Thin wrapper over nebula.web0.
#
# Usage:
#   ./image_pipeline.sh photo.jpg                 # → photo_web0.avif + JSON result
#   ./image_pipeline.sh logo.png banner.png       # batch
#   NEBULA_IMAGE_ARGS="--format webp" ./image_pipeline.sh photo.jpg
#
# Requires: python3 with the nebula package importable and pillow>=11.3.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <image> [image ...]" >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
command -v "${PYTHON}" >/dev/null 2>&1 || PYTHON=python

# shellcheck disable=SC2086 — NEBULA_IMAGE_ARGS is intentionally word-split
exec "${PYTHON}" -m nebula.web0 ${NEBULA_IMAGE_ARGS:-} "$@"
