# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Parad0x Labs
"""
nebula/page.py — compress a whole static site folder for Web0 / .null publishing.

Arweave storage is pay-per-byte, forever.  A .null page is published by
uploading a folder (or single file) to Arweave and pointing the on-chain
registrar record at it — so every image you forgot to compress is money
spent permanently.  This module is the pre-publish step:

    nebula-page ./my-site            # → ./my-site_web0, ready to upload

What it does
------------
1. **Copies** the site folder — the source is never touched.
2. **Re-encodes images to AVIF** (or WebP) with nebula's Web0 settings:
   alpha preserved, EXIF orientation applied, SSIM floor with retry,
   and a never-grow guard (if AVIF isn't smaller, the original stays).
3. Optionally **re-encodes videos** through nebula's scene-aware encoder.
4. **Rewrites references** in HTML/CSS/JS (`src`, `srcset`, `url(...)`,
   anything textual) from the old filenames to the new ones.
5. Writes a **manifest** with per-file SHA-256 proof hashes, quality
   scores, and the Arweave cost estimate — the receipt for what you
   are about to publish.

The output folder is always a working page: files that can't be made
smaller (or can't be safely converted) are kept byte-for-byte.

Public API
----------
    from nebula.page import compress_page, PageResult

    result = compress_page("./my-site")
    print(result.summary())
    # → publish with web0's publish.mjs:
    #    node scripts/publish.mjs ./my-site_web0 --name yourname

CLI
---
    python -m nebula.page ./my-site
    python -m nebula.page ./my-site -o ./out --format webp --include-video

Reference-rewrite limits (honest notes)
---------------------------------------
* References are rewritten by exact text match, per document: the
  root-absolute form ("/img/x.png") and the document-relative form
  ("../img/x.png"), plus the bare filename when it is unique in the
  tree.  Paths built dynamically in JavaScript at runtime (string
  concatenation, template literals) can not be detected; documents using
  ``<base href>`` resolve differently than the rewriter assumes.  Check
  the page locally before publishing.
* Files whose names need URL-encoding (spaces, ``%``, ``#``) are left
  unconverted rather than risk breaking encoded references.
* Animated GIFs are left as-is (the still-image path would drop frames);
  convert them to video manually if you want them smaller.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nebula.web0 import (
    _IMAGE_EXTENSIONS,
    _VIDEO_EXTENSIONS,
    _AR_PER_GB_DEFAULT,
    _AR_USD_DEFAULT,
    Web0EncodeResult,
    encode_image_web0,
    encode_video_web0,
    estimate_arweave_cost,
)

log = logging.getLogger("nebula.page")

__all__ = ["compress_page", "PageResult"]


# Text formats that may reference media files by name.
_TEXT_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".xml", ".svg",
    ".json", ".webmanifest", ".txt", ".md",
}

# Junk that should never be published to permanent storage.  Only VCS/OS
# metadata is excluded — content directories are always kept.
_IGNORED_NAMES = {".git", ".svn", ".hg", ".DS_Store", "Thumbs.db", "desktop.ini"}

_MANIFEST_NAME = "nebula_page_manifest.json"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    """Returned by :func:`compress_page`."""

    output_dir: Path
    files_total: int            # files in the output tree (manifest excluded)
    images_converted: int
    images_kept: int            # grow-guard kept the original bytes
    videos_converted: int
    videos_kept: int
    refs_rewritten: int         # total reference replacements across text files
    rewritten_files: list[str]  # relative paths of text files that changed
    source_bytes: int           # whole source tree
    output_bytes: int           # whole output tree (manifest excluded)
    ratio: float                # source / output — higher = more compression
    arweave_cost_usd_before: float
    arweave_cost_usd_after: float
    arweave_savings_usd: float
    results: list[Web0EncodeResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)    # "relpath — reason"
    warnings: list[str] = field(default_factory=list)
    manifest_path: Optional[Path] = None

    def summary(self) -> str:
        return (
            f"{self.output_dir.name}: {self.files_total} files  "
            f"{self.source_bytes // 1024}KB→{self.output_bytes // 1024}KB "
            f"({self.ratio:.1f}×)  "
            f"images {self.images_converted} converted / {self.images_kept} kept  "
            f"refs rewritten {self.refs_rewritten}  "
            f"arweave ${self.arweave_cost_usd_before:.4f}→${self.arweave_cost_usd_after:.4f} "
            f"(saves ${self.arweave_savings_usd:.4f})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tree_files(root: Path) -> list[Path]:
    """All regular files under *root*, ignoring VCS/OS junk directories."""
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if any(part in _IGNORED_NAMES for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out.append(p)
    return out


def _tree_bytes(files: list[Path]) -> int:
    return sum(p.stat().st_size for p in files)


def _needs_url_encoding(name: str) -> bool:
    """True when *name* would be percent-encoded inside an URL reference."""
    from urllib.parse import quote
    return quote(name, safe="/-_.~") != name


def _is_animated_gif(path: Path) -> bool:
    if path.suffix.lower() != ".gif":
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return bool(getattr(im, "is_animated", False)) and getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Reference rewriting
# ---------------------------------------------------------------------------

def _rewrite_references(
    out_root: Path,
    mapping: dict[str, str],          # old relpath (posix) → new relpath (posix)
    bare_ok: set[str],                # old basenames safe for bare replacement
) -> tuple[int, list[str], list[str]]:
    """
    Replace old media paths with new ones in every text file of the tree.

    Browsers resolve a relative URL against the *referencing document's*
    directory, so each converted media path is matched per text file in the
    two forms that are unambiguous from that file:

    * the site-root-absolute form ("/img/x.png" — leading slash included in
      the match, so it can never collide with a relative reference), and
    * the path relative to the text file's own directory ("../img/x.png",
      "img/x.png" only when that is what the path actually resolves to from
      this file, or bare "x.png" for same-directory references).

    A final bare-basename pass (only for basenames where *every* carrier in
    the tree was converted) catches references assembled with path prefixes
    stripped, e.g. in JS string tables.

    Returns (total_replacements, changed_relpaths, warnings).
    """
    import posixpath

    total = 0
    changed: list[str] = []
    warnings: list[str] = []

    bare_pairs = sorted(
        {
            (old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1])
            for old, new in mapping.items()
            if old.rsplit("/", 1)[-1] in bare_ok
        },
        key=lambda kv: -len(kv[0]),
    )

    for p in _tree_files(out_root):
        if p.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        rel_self = p.relative_to(out_root).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            warnings.append(f"{rel_self} — not UTF-8, references not rewritten")
            continue

        self_dir = posixpath.dirname(rel_self) or "."
        pairs: set[tuple[str, str]] = set()
        for old, new in mapping.items():
            # Root-absolute form — unambiguous from any document.
            pairs.add(("/" + old, "/" + new))
            # Document-relative form — what a bare relative URL in THIS file
            # actually points at.  A root-relative-looking string in a nested
            # document resolves inside that document's directory, so applying
            # the root mapping there would corrupt the reference.
            pairs.add((
                posixpath.relpath(old, start=self_dir),
                posixpath.relpath(new, start=self_dir),
            ))
        # Longest first so "img/a/x.png" wins over "x.png".
        ordered = sorted(pairs, key=lambda kv: -len(kv[0]))

        original = text
        count = 0
        for old, new in ordered:
            n = text.count(old)
            if n:
                text = text.replace(old, new)
                count += n
        for old, new in bare_pairs:
            n = text.count(old)
            if n:
                text = text.replace(old, new)
                count += n

        if text != original:
            p.write_text(text, encoding="utf-8")
            total += count
            changed.append(rel_self)

    return total, changed, warnings


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def compress_page(
    site_dir:       str | Path,
    output_dir:     Optional[str | Path] = None,
    fmt:            str   = "avif",
    quality:        Optional[int]   = None,
    target_ssim:    Optional[float] = None,
    include_video:  bool  = False,
    min_bytes:      int   = 2048,
    measure_quality: bool = True,
    write_manifest: bool  = True,
    force:          bool  = False,
    ffmpeg:         str   = "ffmpeg",
    ffprobe:        str   = "ffprobe",
    ar_per_gb:      float = _AR_PER_GB_DEFAULT,
    ar_usd:         float = _AR_USD_DEFAULT,
) -> PageResult:
    """
    Compress every image (and optionally video) in a static site folder,
    rewrite references, and emit an upload-ready copy plus a proof manifest.

    Parameters
    ----------
    site_dir:
        The source site folder.  Never modified.
    output_dir:
        Destination folder.  Defaults to ``<site_dir>_web0`` next to the
        source.  Must not already exist unless *force* is True, and must
        not be nested inside the source (or vice versa).
    fmt:
        Image target format: "avif" (default) or "webp".
    quality:
        Image quality override (0-100).  Default: per-content-type settings.
    target_ssim:
        SSIM floor for images — encodes landing below it are retried at
        higher quality.
    include_video:
        Also re-encode video files through nebula's scene-aware encoder.
        Significantly slower; off by default.
    min_bytes:
        Files smaller than this are copied untouched (container overhead
        usually beats any savings).
    measure_quality:
        Measure SSIM (images) / VMAF (videos).  Needed for the SSIM floor.
    write_manifest:
        Write ``nebula_page_manifest.json`` (per-file proof hashes, quality,
        cost estimate) into the output folder.
    force:
        Replace an existing output folder.
    ar_per_gb / ar_usd:
        Arweave pricing for the cost estimate.

    Returns
    -------
    PageResult
    """
    site_dir = Path(site_dir).resolve()
    if not site_dir.is_dir():
        raise NotADirectoryError(f"Site folder not found: {site_dir}")

    fmt = fmt.lower().lstrip(".")
    if fmt not in ("avif", "webp"):
        raise ValueError(f"Unsupported format '{fmt}'.  Choose 'avif' or 'webp'.")

    out_root = (Path(output_dir).resolve() if output_dir is not None
                else site_dir.with_name(site_dir.name + "_web0"))
    if out_root == site_dir:
        raise ValueError("Output folder must differ from the site folder.")
    for a, b in ((out_root, site_dir), (site_dir, out_root)):
        try:
            a.relative_to(b)
        except ValueError:
            continue
        raise ValueError(f"'{a}' is nested inside '{b}' — pick a sibling output folder.")

    if out_root.exists():
        if not force:
            raise FileExistsError(
                f"Output folder already exists: {out_root}  (pass force=True / --force)"
            )
        log.info("removing existing output folder %s", out_root)
        shutil.rmtree(out_root)

    src_files = _tree_files(site_dir)
    source_bytes = _tree_bytes(src_files)
    log.info("copying %d files (%d KB) → %s",
             len(src_files), source_bytes // 1024, out_root)
    shutil.copytree(
        site_dir, out_root,
        ignore=shutil.ignore_patterns(*_IGNORED_NAMES),
    )

    all_files = _tree_files(out_root)
    basename_counts: dict[str, int] = {}
    for p in all_files:
        basename_counts[p.name] = basename_counts.get(p.name, 0) + 1

    results:  list[Web0EncodeResult] = []
    skipped:  list[str] = []
    warnings: list[str] = []
    mapping:  dict[str, str] = {}   # old relpath → new relpath (posix)
    images_converted = images_kept = 0
    videos_converted = videos_kept = 0

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------
    skip_suffixes = {f".{fmt}"}
    for p in all_files:
        suffix = p.suffix.lower()
        if suffix not in _IMAGE_EXTENSIONS:
            continue
        rel = p.relative_to(out_root).as_posix()
        if suffix in skip_suffixes:
            skipped.append(f"{rel} — already {fmt}")
            continue
        if p.stat().st_size < min_bytes:
            skipped.append(f"{rel} — under {min_bytes} B, not worth converting")
            continue
        if _needs_url_encoding(rel):
            skipped.append(f"{rel} — name needs URL-encoding, left untouched")
            continue
        if _is_animated_gif(p):
            skipped.append(f"{rel} — animated GIF, convert to video manually")
            continue

        target = p.with_suffix(f".{fmt}")
        if target.exists():
            skipped.append(f"{rel} — {target.name} already exists, left untouched")
            continue

        try:
            r = encode_image_web0(
                source=p, output=target,
                quality=quality, fmt=fmt,
                measure_quality=measure_quality,
                target_ssim=target_ssim,
                keep_original_if_larger=True,
                ar_per_gb=ar_per_gb, ar_usd=ar_usd,
            )
        except Exception as exc:                      # keep the page intact
            warnings.append(f"{rel} — image encode failed: {exc}")
            Path(target).unlink(missing_ok=True)
            continue

        results.append(r)
        if r.encoder == "copy":
            images_kept += 1
            # encode_image_web0 already removed its attempt; original stands.
        else:
            images_converted += 1
            p.unlink()
            mapping[rel] = p.with_suffix(f".{fmt}").relative_to(out_root).as_posix()

    # ------------------------------------------------------------------
    # Videos (opt-in)
    # ------------------------------------------------------------------
    if include_video:
        for p in _tree_files(out_root):
            suffix = p.suffix.lower()
            if suffix not in _VIDEO_EXTENSIONS:
                continue
            rel = p.relative_to(out_root).as_posix()
            if p.stat().st_size < min_bytes:
                skipped.append(f"{rel} — under {min_bytes} B, not worth converting")
                continue
            if _needs_url_encoding(rel):
                skipped.append(f"{rel} — name needs URL-encoding, left untouched")
                continue

            tmp = p.with_name(p.stem + ".w0tmp.mp4")
            try:
                r = encode_video_web0(
                    source=p, output=tmp,
                    ffmpeg=ffmpeg, ffprobe=ffprobe,
                    measure_vmaf=measure_quality,
                    ar_per_gb=ar_per_gb, ar_usd=ar_usd,
                )
                if tmp.stat().st_size >= p.stat().st_size:
                    tmp.unlink(missing_ok=True)
                    videos_kept += 1
                    skipped.append(f"{rel} — re-encode not smaller, original kept")
                    continue
                results.append(r)
                videos_converted += 1
                if suffix == ".mp4":
                    p.unlink()
                    tmp.replace(p)                    # same name — no rewrite
                    r.output_path = p                 # manifest must not show the tmp name
                else:
                    new = p.with_suffix(".mp4")
                    if new.exists():
                        tmp.unlink(missing_ok=True)
                        skipped.append(f"{rel} — {new.name} already exists, left untouched")
                        videos_converted -= 1
                        results.pop()
                        continue
                    p.unlink()
                    tmp.replace(new)
                    r.output_path = new
                    mapping[rel] = new.relative_to(out_root).as_posix()
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                warnings.append(f"{rel} — video encode failed: {exc}")

    # ------------------------------------------------------------------
    # Rewrite references
    # ------------------------------------------------------------------
    # A bare basename may be replaced only when every file in the tree that
    # carried this basename was converted — otherwise an untouched sibling
    # (e.g. a second img/logo.png elsewhere) would have its references broken.
    converted_basename_counts: dict[str, int] = {}
    for old in mapping:
        name = old.rsplit("/", 1)[-1]
        converted_basename_counts[name] = converted_basename_counts.get(name, 0) + 1
    bare_ok = {
        name for name, n in converted_basename_counts.items()
        if basename_counts.get(name, 0) == n
    }

    refs_rewritten, rewritten_files, rw_warnings = _rewrite_references(
        out_root, mapping, bare_ok
    )
    warnings.extend(rw_warnings)
    for old in mapping:
        name = old.rsplit("/", 1)[-1]
        if name not in bare_ok:
            warnings.append(
                f"{old} — duplicate basename in tree; only full-path references "
                "were rewritten (same-directory bare references may need a manual fix)"
            )

    # ------------------------------------------------------------------
    # Totals + manifest
    # ------------------------------------------------------------------
    out_files = _tree_files(out_root)
    output_bytes = _tree_bytes(out_files)
    ratio = (source_bytes / output_bytes) if output_bytes else 0.0
    cost_before = estimate_arweave_cost(source_bytes, ar_per_gb, ar_usd)
    cost_after  = estimate_arweave_cost(output_bytes, ar_per_gb, ar_usd)
    savings_usd = round(cost_before["usd"] - cost_after["usd"], 6)

    manifest_path: Optional[Path] = None
    if write_manifest:
        import json
        manifest = {
            "generator":   "nebula-media page mode",
            "site":        site_dir.name,
            "format":      fmt,
            "created_unix": int(time.time()),
            "totals": {
                "files":         len(out_files),
                "source_bytes":  source_bytes,
                "output_bytes":  output_bytes,
                "ratio":         round(ratio, 3),
                "arweave_cost_usd_before": cost_before["usd"],
                "arweave_cost_usd_after":  cost_after["usd"],
                "arweave_savings_usd":     savings_usd,
                "ar_per_gb":     ar_per_gb,
                "ar_usd":        ar_usd,
            },
            "images": {"converted": images_converted, "kept": images_kept},
            "videos": {"converted": videos_converted, "kept": videos_kept},
            "refs_rewritten": refs_rewritten,
            "rewritten_files": rewritten_files,
            "files": [
                {
                    "path":          r.output_path.relative_to(out_root).as_posix(),
                    "action":        "kept" if r.encoder == "copy" else f"converted:{r.encoder}",
                    "bytes_before":  r.source_size,
                    "bytes_after":   r.output_size,
                    "quality_metric": "vmaf" if r.encoder in ("x265", "svt-av1", "vvc", "videotoolbox") else "ssim",
                    "quality":       r.quality_score,
                    "proof_hash":    r.proof_hash,
                }
                for r in results
            ],
            "skipped":  skipped,
            "warnings": warnings,
            "notes": [
                "proof_hash = SHA-256 of the exact output file bytes.",
                "Arweave cost is an estimate; the gateway/bundler quotes the real price at upload time.",
                "The manifest itself is not counted in output totals.",
            ],
        }
        manifest_path = out_root / _MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = PageResult(
        output_dir       = out_root,
        files_total      = len(out_files),
        images_converted = images_converted,
        images_kept      = images_kept,
        videos_converted = videos_converted,
        videos_kept      = videos_kept,
        refs_rewritten   = refs_rewritten,
        rewritten_files  = rewritten_files,
        source_bytes     = source_bytes,
        output_bytes     = output_bytes,
        ratio            = round(ratio, 2),
        arweave_cost_usd_before = cost_before["usd"],
        arweave_cost_usd_after  = cost_after["usd"],
        arweave_savings_usd     = savings_usd,
        results          = results,
        skipped          = skipped,
        warnings         = warnings,
        manifest_path    = manifest_path,
    )
    log.info("page done: %s", result.summary())
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="nebula-page",
        description="Compress a static site folder for Web0/.null publishing "
                    "(images → AVIF, references rewritten, proof manifest).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("site", help="Site folder to compress (never modified)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output folder (default: <site>_web0)")
    parser.add_argument("--format", "-f", default="avif",
                        choices=["avif", "webp"],
                        help="Image target format")
    parser.add_argument("--quality", "-q", type=int, default=None,
                        help="Image quality override (0-100)")
    parser.add_argument("--target-ssim", type=float, default=None,
                        dest="target_ssim", metavar="SSIM",
                        help="SSIM floor for images (e.g. 0.96)")
    parser.add_argument("--include-video", action="store_true",
                        help="Also re-encode videos (much slower)")
    parser.add_argument("--min-bytes", type=int, default=2048,
                        help="Skip files smaller than this")
    parser.add_argument("--force", action="store_true",
                        help="Replace the output folder if it exists")
    parser.add_argument("--no-manifest", action="store_true",
                        help="Do not write nebula_page_manifest.json")
    parser.add_argument("--no-quality", action="store_true",
                        help="Skip SSIM/VMAF measurement (faster, disables the SSIM floor)")
    parser.add_argument("--ar-per-gb", type=float, default=_AR_PER_GB_DEFAULT,
                        metavar="AR", help="Arweave storage cost in AR per GB")
    parser.add_argument("--ar-usd", type=float, default=_AR_USD_DEFAULT,
                        metavar="USD", help="AR/USD rate for the cost estimate")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("nebula").setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)

    try:
        r = compress_page(
            site_dir       = args.site,
            output_dir     = args.output,
            fmt            = args.format,
            quality        = args.quality,
            target_ssim    = args.target_ssim,
            include_video  = args.include_video,
            min_bytes      = args.min_bytes,
            measure_quality = not args.no_quality,
            write_manifest = not args.no_manifest,
            force          = args.force,
            ffmpeg         = args.ffmpeg,
            ffprobe        = args.ffprobe,
            ar_per_gb      = args.ar_per_gb,
            ar_usd         = args.ar_usd,
        )
    except Exception as exc:
        log.error("%s", exc)
        return 1

    print(json.dumps({
        "output_dir":        str(r.output_dir),
        "files":             r.files_total,
        "images_converted":  r.images_converted,
        "images_kept":       r.images_kept,
        "videos_converted":  r.videos_converted,
        "videos_kept":       r.videos_kept,
        "refs_rewritten":    r.refs_rewritten,
        "rewritten_files":   r.rewritten_files,
        "source_kb":         r.source_bytes // 1024,
        "output_kb":         r.output_bytes // 1024,
        "ratio":             r.ratio,
        "arweave_cost_usd_before": r.arweave_cost_usd_before,
        "arweave_cost_usd_after":  r.arweave_cost_usd_after,
        "arweave_savings_usd":     r.arweave_savings_usd,
        "skipped":           r.skipped,
        "warnings":          r.warnings,
        "manifest":          str(r.manifest_path) if r.manifest_path else None,
        "next_step":         "upload the folder with web0's publish flow, e.g. "
                             "node scripts/publish.mjs <output_dir> --name <yourname>",
    }, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
