# nebula-media

![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![FFmpeg 7.1+](https://img.shields.io/badge/FFmpeg-7.1%2B-green?style=flat-square)

Perceptual video re-encoding pipeline. Detects scene boundaries by visual complexity, assigns per-scene CRF to hit a target VMAF floor, re-muxes the output. Built on FFmpeg and libvmaf.

**Not lossless. Not a codec. Not magic.** It is a scheduling layer that gives each scene the CRF it needs to hit your VMAF target while minimising bitrate. The underlying encode is x265 or SVT-AV1 via FFmpeg subprocess.

---

## Benchmarks

All reproducible. See `proof-pack/` for exact commands and raw VMAF JSON.

**Source matters** — these are high-bitrate camera sources, not already-compressed test vectors. Re-encoding a 25 Mbps test clip with CRF 24 produces a *larger* file — correct behaviour.

| Source | Mode | VMAF | Ratio | Output |
|--------|------|------|-------|--------|
| Jellyfish 1080p 60fps | safe | **88.1** | 12.3x | 2.43 MB from 30 MB |
| Sintel 1080p 24fps | safe | **96.4** | 12.9x | 2.33 MB from 30 MB |
| Sintel 1080p 24fps | extreme | **68.7** | 22.6x | 1.33 MB from 30 MB |

```bash
bash proof-pack/encode_jellyfish_safe.sh   # reproduce any result
```

---

## Install

```bash
pip install nebula-media
```

Requires Python 3.10+, FFmpeg 7.1+ on PATH built with libvmaf. See `scripts/install_deps.sh`.

---

## Quick start

```python
from nebula import compress_video

result = compress_video("input.mp4", mode="safe", target_vmaf=88)
print(f"VMAF: {result.vmaf:.1f}  ratio: {result.ratio:.1f}x")
print(f"Proof: {result.proof_hash}")  # SHA-256 of output for on-chain anchoring
```

| Mode | VMAF target | Use case |
|------|-------------|----------|
| `safe` | 88+ | Archival, professional |
| `balanced` | 74+ | Streaming, web |
| `extreme` | 65+ | Storage-constrained |
| `gladiator` | custom | Auto-picks best encoder per scene |

---

## On-chain quality proof (optional)

```python
result = compress_video(
    "input.mp4", mode="safe",
    x402_keypair_path="~/.config/solana/id.json",
)
print(result.receipt["signature"])  # Solana tx: VMAF + SHA-256 on-chain
```

1,000 encode receipts → one 32-byte Merkle root on Solana → $0.001 total.  
Via [DNA x402](https://github.com/Parad0x-Labs/dna-x402) + [Liquefy](https://github.com/Parad0x-Labs/liquefy).

---

## How it works

```
input.mp4
  → scene detection  (ffmpeg scdet)
  → per-scene CRF assignment (complex = lower CRF = more bits)
  → encode: x265 or SVT-AV1 with zone params
  → VMAF measurement (libvmaf)
  → if VMAF < target: adjust + re-encode (max 2 passes)
  → output.mp4 + proof_hash (SHA-256)
```

---

## Nebula + Liquefy + DNA x402

Nebula re-encodes. [Liquefy](https://github.com/Parad0x-Labs/liquefy) compresses the encode receipts and builds a streaming Merkle tree. [DNA x402](https://github.com/Parad0x-Labs/dna-x402) gates the API behind USDC payment and anchors the Merkle root on Solana. Together: pay for an encode → get the video + a chain-verifiable proof that it was encoded to a specific VMAF standard.

---

## Requirements

- Python 3.10+, FFmpeg 7.1+ (`--enable-libvmaf`), libvmaf 3.0+, x265 3.6+
- Optional on-chain receipts: `pip install "nebula-media[solana]"`

---

© 2026 Parad0x Labs — MIT License
