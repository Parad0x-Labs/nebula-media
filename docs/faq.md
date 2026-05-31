# Frequently Asked Questions

### 1. Is this open source?
Yes — **MIT**. The scene-aware encoder (`nebula/encoder.py`), the on-chain proof bridge
(`nebula/receipt.py`), the pipelines, and the verification tools are all open and in this
repository. No sealed binaries, no withheld kernels. Clone it, run it, own it.

### 2. Is it actually scene-aware, or just one quality setting?
Scene-aware. `nebula/encoder.py` detects scene cuts, builds per-zone CRF (complex scenes
get more bits, near-static scenes fewer), snaps zone edges to keyframe boundaries, and
auto-selects x265 vs SVT-AV1. It measures VMAF mean **and** 1st-percentile.

### 3. What's the "proof" / receipt?
Each encode can mint a 32-byte commitment binding the exact output bytes + VMAF + ratio +
timestamp. The receipt is compressed with Liquefy, archived to Arweave, and the commitment
is anchored on Solana mainnet via the `receipt_anchor` program — the same rail dna-x402
uses. Anchoring is optional (bring your own keypair); without one you still get the local
SHA-256 proof.

### 4. Can I reproduce the benchmarks?
Yes. Source clips, exact encode scripts, and output references are provided — run
`bash proof-pack/encode_jellyfish_safe.sh` (reproduces within ±0.5 VMAF). The published
reference (Jellyfish 1080p, x265) lands at VMAF 88.1, ~2.43 MB.

### 5. Is this production-ready?
**Alpha.** The encoder and the proof bridge are implemented and the benchmark passes, but
it hasn't been hardened across every codec build or scaled to a managed service. Image/AVIF
and audio pipelines are early stubs; video is the live path. We say so out loud.
