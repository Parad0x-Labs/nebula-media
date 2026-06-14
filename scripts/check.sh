#!/usr/bin/env bash
# nebula-media setup verifier ("doctor").
# Checks Python deps, ffmpeg, codec availability, and runs a real smoke encode.
# Does NOT install or mutate anything — it tells you exactly what to fix.
# Safe to re-run. Exit 0 = ready for at least one mode; exit 1 = nothing works.

set -u
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }

echo "== nebula-media check =="

# --- Python ---------------------------------------------------------------
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then err "python3 not found — install Python 3.10+"; exit 1; fi
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
MAJ="${PYV%%.*}"; MIN="${PYV##*.}"
if [ "$MAJ" -gt 3 ] || { [ "$MAJ" -eq 3 ] && [ "$MIN" -ge 10 ]; }; then
  ok "python $PYV ($PY)"
else
  warn "python $PYV — project targets 3.10+ (may still run on 3.9)"
fi

# --- Python deps ----------------------------------------------------------
DEPS_OK=1
for mod in numpy scipy PIL psutil; do
  if "$PY" -c "import $mod" 2>/dev/null; then ok "python dep: $mod"; else err "python dep MISSING: $mod"; DEPS_OK=0; fi
done
if "$PY" -c "import nebula" 2>/dev/null || PYTHONPATH=. "$PY" -c "import nebula" 2>/dev/null; then
  ok "nebula package imports"
else
  err "nebula does not import — run:  pip install -e .  (in a venv)"; DEPS_OK=0
fi
[ "$DEPS_OK" -eq 0 ] && warn "fix deps with:  python3 -m venv .venv && source .venv/bin/activate && pip install -e ."

# --- AVIF (image mode) ----------------------------------------------------
if "$PY" -c "from PIL import features,Image; import sys; sys.exit(0 if features.check('avif') else 1)" 2>/dev/null; then
  ok "image mode ready (Pillow + AVIF) — no ffmpeg needed"
else
  warn "Pillow lacks AVIF support — upgrade: pip install -U 'pillow>=11.3'"
fi

# --- ffmpeg (video modes) -------------------------------------------------
VIDEO_OK=0
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg + ffprobe on PATH ($(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3))"
  ENC="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
  FILT="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  if echo "$ENC" | grep -q libx264; then ok "H.264 / libx264 — '--target x' (Twitter) ready"; VIDEO_OK=1
  else err "libx264 MISSING — '--target x' (X uploads) will not work"; fi
  echo "$ENC" | grep -q libx265   && ok "HEVC / libx265"                  || warn "libx265 missing — x265 path unavailable"
  echo "$ENC" | grep -q libsvtav1 && ok "AV1 / libsvtav1"                 || warn "libsvtav1 missing — AV1 (default) path unavailable"
  echo "$ENC" | grep -q libvvenc  && ok "VVC / libvvenc (optional)"        || warn "libvvenc missing — '--encoder vvc' unavailable (optional)"
  echo "$ENC" | grep -q hevc_videotoolbox && ok "Apple VideoToolbox (hardware HEVC)" || true
  echo "$FILT" | grep -q libvmaf  && ok "VMAF / libvmaf"                   || warn "libvmaf missing — quality scores return -1 (encodes still work)"

  # --- smoke test: encode a 1s clip via --target x -----------------------
  if [ "$VIDEO_OK" -eq 1 ] && [ "$DEPS_OK" -eq 1 ]; then
    TMP="$(mktemp -d)"
    if ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=320x240:duration=1:rate=30" \
         -c:v libx264 "$TMP/in.mp4" 2>/dev/null \
       && PYTHONPATH="${PYTHONPATH:-.}" "$PY" -m nebula.web0 "$TMP/in.mp4" -o "$TMP/out.mp4" \
            --target x --no-quality >/dev/null 2>&1 \
       && [ -s "$TMP/out.mp4" ]; then
      ok "smoke test passed — encoded a clip via --target x"
    else
      err "smoke test FAILED — video encode path is broken; check the error above"
      VIDEO_OK=0
    fi
    rm -rf "$TMP"
  fi
else
  warn "ffmpeg/ffprobe NOT on PATH — video modes won't run (image + page modes still work)"
  warn "install:  macOS: brew install ffmpeg  |  Ubuntu: sudo apt install ffmpeg  |  https://ffmpeg.org/download.html"
fi

# --- verdict --------------------------------------------------------------
echo ""
if [ "$DEPS_OK" -eq 1 ] && [ "$VIDEO_OK" -eq 1 ]; then
  echo "== READY (video + images) =="
  echo "Try:  python -m nebula.web0 yourclip.mp4 --target x"
  exit 0
elif [ "$DEPS_OK" -eq 1 ]; then
  echo "== READY for images/pages — install ffmpeg for video =="
  echo "Try:  python -m nebula.web0 yourphoto.png"
  exit 0
else
  echo "== NOT READY — install deps above, then re-run: bash scripts/check.sh =="
  exit 1
fi
