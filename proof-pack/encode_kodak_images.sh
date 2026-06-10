#!/usr/bin/env bash
# encode_kodak_images.sh
# Reproduces the Kodak AVIF image benchmark from the nebula-media proof-pack.
#
# Pipeline under test: nebula.web0.encode_image_web0 (Pillow >= 11.3 + libavif).
# No ffmpeg needed — the image path is pure Python.
#
# Requirements:
#   python3 with: pillow>=11.3, numpy, scipy  (pip install -e . from repo root)
#   curl (only for the first run, to download the two Kodak source images)
#
# Usage:
#   bash proof-pack/encode_kodak_images.sh
#
# Sources are downloaded into proof-pack/sources/ (gitignored) and verified
# by SHA-256 before encoding. Expected numbers + tolerances live in
# proof-pack/results/kodak_images_expected.json.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${REPO_ROOT}/proof-pack/sources"
EXPECTED="${REPO_ROOT}/proof-pack/results/kodak_images_expected.json"
cd "${REPO_ROOT}"

PYTHON="${PYTHON:-python3}"
command -v "${PYTHON}" >/dev/null 2>&1 || PYTHON=python

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
"${PYTHON}" - <<'PY'
from PIL import features
import PIL
assert features.check("avif"), (
    f"Pillow {PIL.__version__} has no AVIF support — pip install --upgrade 'pillow>=11.3'"
)
print(f"Pillow {PIL.__version__} with AVIF: OK")
PY

# ---------------------------------------------------------------------------
# Step 1 — Download + verify sources
# ---------------------------------------------------------------------------
mkdir -p "${SRC_DIR}"
"${PYTHON}" - "$EXPECTED" "$SRC_DIR" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path

expected = json.loads(Path(sys.argv[1]).read_text())
src_dir = Path(sys.argv[2])

for src in expected["sources"]:
    dest = src_dir / src["file"]
    if not dest.exists():
        print(f"downloading {src['file']} …")
        subprocess.run(["curl", "-sL", "-o", str(dest), src["url"]], check=True)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != src["sha256"]:
        sys.exit(f"FAIL: {src['file']} sha256 mismatch\n  expected {src['sha256']}\n  got      {digest}")
    print(f"  {src['file']}: {dest.stat().st_size} B  sha256 OK")
PY

# ---------------------------------------------------------------------------
# Step 2 — Encode + compare against expected
# ---------------------------------------------------------------------------
"${PYTHON}" - "$EXPECTED" "$SRC_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path

expected = json.loads(Path(sys.argv[1]).read_text())
src_dir = Path(sys.argv[2])
tol = expected["reproduction_tolerance"]
warned = False

for case in expected["cases"]:
    src = src_dir / case["source"]
    out = src_dir / f"{src.stem}_{case['content_type']}.avif"
    proc = subprocess.run(
        [sys.executable, "-m", "nebula.web0", str(src),
         "--content-type", case["content_type"], "-o", str(out)],
        capture_output=True, text=True, check=True,
    )
    r = json.loads(proc.stdout)
    exp = case["expected"]
    got_bytes = out.stat().st_size
    got_ssim = r["quality"]

    size_pct = abs(got_bytes - exp["output_bytes"]) / exp["output_bytes"] * 100
    ssim_delta = abs(got_ssim - exp["ssim"])
    ok = size_pct <= tol["size_delta_pct_max"] and ssim_delta <= tol["ssim_delta_max"]
    status = "PASS" if ok else "WARN"
    warned |= not ok

    print(f"{status}  {case['source']} {case['content_type']:<10} q={case['avif_quality']}  "
          f"{got_bytes} B (exp {exp['output_bytes']}, d={size_pct:.1f}%)  "
          f"SSIM {got_ssim:.4f} (exp {exp['ssim']:.4f}, d={ssim_delta:.4f})")

print()
if warned:
    print("WARN - at least one case landed outside tolerance. Check your Pillow/libavif")
    print("version (reference: Pillow 12.2.0) and open an issue with the output above.")
else:
    print("PASS - all cases reproduced within tolerance (size +-5%, SSIM +-0.005).")
PY

echo
echo "Done."
