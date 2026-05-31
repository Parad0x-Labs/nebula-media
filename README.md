# nebula-media

Perceptual video re-encoding. Scene-aware CRF. VMAF quality proofs. MIT.

Splits video by scene complexity, assigns per-scene CRF to hit a VMAF target, re-muxes. Not lossless. x265 or SVT-AV1 via FFmpeg.

### How this fits the Parad0x stack

Parad0x Labs builds Web0 on Solana — money and agents that settle themselves. **You are here: 🎬 Media.**

| Layer | Repo | Does |
|---|---|---|
| 💸 Payments | [dna-x402](https://github.com/Parad0x-Labs/dna-x402) | x402 rail: quote → pay → verify → receipt → anchor |
| 🛠️ Build | [dna-x402-builders](https://github.com/Parad0x-Labs/dna-x402-builders) | Hosted kit: turn any API/bot into a paid agent |
| 🕶️ Privacy | [Dark-Null-Protocol](https://github.com/Parad0x-Labs/Dark-Null-Protocol) | Groth16 privacy settlement, published proofs |
| 🗜️ Data | [liquefy](https://github.com/Parad0x-Labs/liquefy) | Columnar compression that beats Zstd + audit trails |
| 🎬 Media | [nebula-media](https://github.com/Parad0x-Labs/nebula-media) (this repo) | Perceptual video re-encoding, VMAF quality proofs |
| 🧠 Local AI | [nulla-local](https://github.com/Parad0x-Labs/nulla-local) | Local-first agent runtime — your machine, your memory |

**See it live**: [parad0xlabs.com](https://parad0xlabs.com)

---

See the full documentation in this repository.

**License:** MIT — © 2026 Parad0x Labs
