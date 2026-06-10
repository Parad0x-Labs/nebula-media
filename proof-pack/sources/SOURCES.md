# Benchmark Source Files

All source files used in the proof-pack benchmarks are listed here with download
instructions. Reproducers must use these exact files. Encoding a different file of
the same name will produce different quality numbers.

---

## Kodak still images (image/AVIF benchmark)

The classic Kodak Photo CD test set — uncompressed-origin PNGs, public test
images used by virtually every image-codec paper. `encode_kodak_images.sh`
downloads them into this directory automatically and verifies these hashes
before encoding:

| File        | URL                                              | Size      | SHA-256 |
|-------------|--------------------------------------------------|-----------|---------|
| kodim23.png | https://r0k.us/graphics/kodak/kodak/kodim23.png | 557,596 B | `e3111a2fd4da24af15d6459ef9eacfe54106b38e27b4a21821b75c3f5d2d5baf` |
| kodim13.png | https://r0k.us/graphics/kodak/kodak/kodim13.png | 822,712 B | `bc34a3ce58dea09dce1704c997171602de90cb34d0c8503a988b77f473d39b08` |

kodim23 (parrots) is a moderate-detail photo; kodim13 (river rapids) is the
hardest image in the set — together they bracket the realistic ratio range.
Downloaded PNGs and generated AVIFs in this directory are gitignored.

---

## Jellyfish 1080p 10-second clip

| Field       | Value                                    |
|-------------|------------------------------------------|
| URL         | https://files.catbox.moe/d5r7i6.mp4     |
| Duration    | ~10 s                                    |
| Resolution  | 1920 x 1080                             |
| Source size | ~30 MB                                  |
| SHA-256     | to be verified after download (run `sha256sum jellyfish_1080_10s.mp4`) |

Download:

```
curl -L -o jellyfish_1080_10s.mp4 https://files.catbox.moe/d5r7i6.mp4
```

Place the file at `G:\media compress\jellyfish_1080_10s.mp4` (default path used by
`encode_jellyfish_safe.sh`) or export the `JELLYFISH_PATH` environment variable to
point at the actual location.

---

## Big Buck Bunny 1080p

| Field       | Value                                                                                 |
|-------------|---------------------------------------------------------------------------------------|
| URL         | https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4   |
| Duration    | ~10 min 34 s                                                                          |
| Resolution  | 1920 x 1080                                                                           |
| License     | Creative Commons Attribution 3.0 — Blender Foundation                                |

Download:

```
curl -L -o bbb_1080p.mp4 \
  https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4
```

---

## Sintel (clip used for speed benchmark)

| Field       | Value                                                      |
|-------------|------------------------------------------------------------|
| URL         | https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv |
| License     | Creative Commons Attribution 3.0 — Blender Foundation     |

Download:

```
curl -L -o sintel_1080p.mkv \
  https://download.blender.org/durian/movies/Sintel.2010.1080p.mkv
```

The Sintel benchmark measures encode speed (reported as x-realtime). The speed factor
of 12.9x was measured on the full film; a short clip will yield similar results on
the same hardware.

---

## Notes on reproducibility

- All clips are freely licensed for benchmark use.
- SHA-256 hashes should be recorded locally after first download and committed to
  this file if you are maintaining a fork of this repository.
- catbox.moe links are not guaranteed permanent. If the Jellyfish URL is dead,
  any 10-second 1080p H.264 clip of "jellyfish" nature footage encoded at a similar
  bitrate (~30 MB / 10 s) will yield comparable VMAF scores, but exact numbers will
  differ slightly.
