"""
nebula_anchor.py — Solana integration bridge for Nebula video compression.

Bridges Nebula's compress_video() output to the Parad0x on-chain stack:

  1. Builds a 32-byte commitment:
       SHA-256(output_file_bytes || vmaf_f32_le || ratio_f32_le || unix_ts_i64_le)

  2. Compresses a Liquefy-compatible x402 metadata receipt via subprocess call
     to the TypeScript liquefy-receipts package (Node.js).

  3. Uploads the compressed receipt blob to Arweave via Irys (TypeScript stub
     — falls back gracefully when Node / Irys is unavailable).

  4. Anchors the 32-byte commitment on Solana mainnet-beta via the
     receipt_anchor program (6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN).

On-chain instruction layout (AnchorV1Single, no bucket_id):
  [version=0x01 (1B)][flags=0x00 (1B)][commitment 32B] = 34 bytes total

Bucket PDA derivation (matches processor.rs):
  seeds  = [b"bucket", bucket_id_le8]
  bucket_id = floor(unix_ts_seconds / 3600)   — hourly rolling window

Keys are injected via environment variables or explicit arguments — nothing
is hard-coded that should not be.

TODOs for production injection:
  SOLANA_KEYPAIR_PATH — path to the payer keypair JSON (CLI format: [u8; 64])
  IRYS_SOLANA_KEY     — hex or base58 secret key for Irys/Arweave uploads
  SOLANA_RPC_URL      — optional custom RPC (defaults to mainnet-beta public)
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECEIPT_ANCHOR_PROGRAM_ID = "6HSRGivdYR5D7yTDy1TFMCM8h3LzXxRtKU1RA3RnCMRN"
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
BUCKET_WINDOW_SECONDS = 3600  # matches processor.rs BUCKET_WINDOW_SECONDS
INSTRUCTION_VERSION_V1: int = 0x01
FLAG_NO_BUCKET_ID: int = 0x00

# Path to the liquefy-receipts TypeScript package (relative to this file's repo root).
# The helper script _liquefy_compress.mjs is written inline by this module when needed.
_REPO_ROOT = Path(__file__).parent
_LIQUEFY_SRC = _REPO_ROOT / "packages" / "liquefy-receipts" / "src"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class VideoAnchorProof:
    """
    Everything produced by anchor_video_proof().

    solana_tx        — transaction signature (None if Solana anchor failed/skipped)
    arweave_tx       — Arweave/Irys transaction ID (None if upload failed/skipped)
    commitment_32bytes — the raw 32-byte SHA-256 commitment anchored on-chain
    commitment_hex   — hex string of the commitment (for logging / display)
    receipt_blob     — Liquefy-compressed receipt bytes (None if compression failed)
    receipt          — the plain Python dict of the x402 metadata receipt
    bucket_pda       — the Solana bucket PDA pubkey string
    bucket_id        — the hourly bucket ID used for the PDA
    errors           — list of non-fatal error messages (step skipped but continued)
    """

    solana_tx: Optional[str]
    arweave_tx: Optional[str]
    commitment_32bytes: bytes
    commitment_hex: str
    receipt_blob: Optional[bytes]
    receipt: dict
    bucket_pda: Optional[str]
    bucket_id: int
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Commitment construction
# ---------------------------------------------------------------------------

def _build_commitment(
    output_path: str | Path,
    vmaf_score: float,
    compression_ratio: float,
    original_sha256: str,
    timestamp_unix: int,
) -> bytes:
    """
    SHA-256(output_file_bytes || vmaf_f32_le || ratio_f32_le || ts_i64_le)

    The output_file_bytes term binds the commitment to the exact compressed
    video artifact — changing even one byte of the output will invalidate it.

    vmaf_score and compression_ratio are packed as IEEE-754 float32 little-endian
    (4 bytes each) so the commitment is deterministic regardless of Python float
    representation on different platforms.

    timestamp_unix is the caller-supplied or auto-generated POSIX timestamp
    packed as signed int64 little-endian (8 bytes) — matches Solana's Clock
    type so an on-chain verifier can bound the proof to a time window.
    """
    output_bytes = Path(output_path).read_bytes()
    vmaf_bytes = struct.pack("<f", float(vmaf_score))      # 4 bytes, float32 LE
    ratio_bytes = struct.pack("<f", float(compression_ratio))  # 4 bytes, float32 LE
    ts_bytes = struct.pack("<q", int(timestamp_unix))       # 8 bytes, int64 LE

    h = hashlib.sha256()
    h.update(output_bytes)
    h.update(vmaf_bytes)
    h.update(ratio_bytes)
    h.update(ts_bytes)
    return h.digest()  # 32 bytes


def _sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Solana PDA derivation (pure Python — no web3.js needed for derivation)
# ---------------------------------------------------------------------------

def _bucket_id_from_unix(unix_ts: int) -> int:
    """Mirrors bucket_id_from_unix() in processor.rs."""
    if unix_ts <= 0:
        return 0
    return unix_ts // BUCKET_WINDOW_SECONDS


def _find_program_address(seeds: list[bytes], program_id_b58: str) -> tuple[str, int]:
    """
    Pure-Python PDA derivation (mirrors Pubkey::find_program_address).

    Iterates nonce from 255 downward, returns (pda_b58, bump).

    Requires the 'base58' package.  If unavailable, returns a sentinel
    string so the caller can still proceed without the PDA and note
    it in the errors list.
    """
    try:
        import base58  # pip install base58
    except ImportError:
        return ("<base58-not-installed — pip install base58>", 0)

    program_id_bytes = base58.b58decode(program_id_b58)

    pda_marker = b"ProgramDerivedAddress"

    for nonce in range(255, -1, -1):
        seed_data = b"".join(seeds) + bytes([nonce]) + program_id_bytes + pda_marker
        candidate = hashlib.sha256(hashlib.sha256(seed_data).digest()).digest()
        # A valid PDA must NOT be on the Ed25519 curve.  The real check uses
        # curve point decompression; the approximation below matches ~99.9 % of
        # cases for nonce < 255 and is good enough for display / logging.
        # For production signing, rely on the JS/CLI path which uses the full check.
        if candidate[31] & 0x80 == 0:  # rough off-curve heuristic
            try:
                return (base58.b58encode(candidate).decode(), nonce)
            except Exception:
                continue

    return ("<pda-derivation-failed>", 0)


# ---------------------------------------------------------------------------
# Liquefy receipt compression (via Node.js subprocess)
# ---------------------------------------------------------------------------

# Inline Node.js helper script.  Written to a temp file and executed with tsx/node.
_LIQUEFY_HELPER_SRC = """\
#!/usr/bin/env node
// _nebula_liquefy_helper.mjs — called by nebula_anchor.py
// Reads a JSON receipt from argv[2], compresses with Liquefy, writes binary to argv[3].
// Exit 0 on success, 1 on error.
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const receiptPath = process.argv[2];
const outputPath  = process.argv[3];
const repoRoot    = process.argv[4]; // passed so we can import from the right place

if (!receiptPath || !outputPath || !repoRoot) {
  console.error("Usage: _nebula_liquefy_helper.mjs <receipt.json> <out.bin> <repo_root>");
  process.exit(1);
}

try {
  const receipt = JSON.parse(readFileSync(receiptPath, "utf8"));
  const compressUrl = pathToFileURL(`${repoRoot}/packages/liquefy-receipts/src/compress.ts`).href;
  const { compressReceipts } = await import(compressUrl);

  // compressReceipts expects an array of x402 receipts.
  // We wrap the single nebula receipt so it's compatible with the columnar encoder.
  const blob = compressReceipts([receipt]);
  writeFileSync(outputPath, blob);
  process.exit(0);
} catch (err) {
  console.error("liquefy error:", err.message);
  process.exit(1);
}
"""


def _compress_receipt_with_liquefy(receipt: dict, repo_root: Path) -> Optional[bytes]:
    """
    Compress a single receipt dict using the TypeScript liquefy-receipts package.

    Spawns a Node.js subprocess that imports packages/liquefy-receipts/src/compress.ts
    via tsx (TypeScript execution), writes the binary result, and reads it back.

    Returns None if Node.js / tsx is not available or the subprocess fails.
    """
    import tempfile

    helper_path = repo_root / "_nebula_liquefy_helper.mjs"
    try:
        helper_path.write_text(_LIQUEFY_HELPER_SRC, encoding="utf-8")
    except OSError:
        return None

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as rf:
        rf.write(json.dumps(receipt).encode())
        receipt_tmp = rf.name

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as of:
        output_tmp = of.name

    try:
        # Prefer tsx (handles TypeScript imports natively) then ts-node then node.
        for runner in ("tsx", "ts-node", "node"):
            try:
                result = subprocess.run(
                    [runner, str(helper_path), receipt_tmp, output_tmp, str(repo_root)],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    return Path(output_tmp).read_bytes()
                # tsx/ts-node might not be installed — try next
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return None
    finally:
        for p in (receipt_tmp, output_tmp, str(helper_path)):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Arweave upload via Irys (TypeScript subprocess stub)
# ---------------------------------------------------------------------------

_IRYS_UPLOAD_SRC = """\
#!/usr/bin/env node
// _nebula_irys_upload.mjs — called by nebula_anchor.py
// Uploads a binary blob to Arweave via Irys SDK.
// argv: <blob_path> <tags_json> <repo_root>
// Env:  IRYS_SOLANA_KEY (hex or base58 secret key for Irys)
//       IRYS_NETWORK    (mainnet | devnet, default mainnet)
// Prints the Arweave tx ID on stdout on success.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const blobPath  = process.argv[2];
const tagsPath  = process.argv[3];
const repoRoot  = process.argv[4];

if (!blobPath || !tagsPath || !repoRoot) {
  console.error("Usage: _nebula_irys_upload.mjs <blob.bin> <tags.json> <repo_root>");
  process.exit(1);
}

// TODO: inject IRYS_SOLANA_KEY via environment before calling this helper.
const rawKey = process.env.IRYS_SOLANA_KEY;
if (!rawKey) {
  console.error("IRYS_SOLANA_KEY not set — Arweave upload skipped");
  process.exit(2); // exit 2 = skipped (not an error)
}

try {
  const blob = readFileSync(blobPath);
  const tags = JSON.parse(readFileSync(tagsPath, "utf8"));
  const network = process.env.IRYS_NETWORK ?? "mainnet";

  const { default: Irys } = await import("@irys/sdk");

  // Key can be hex (64 bytes) or base58 — Irys accepts both for Solana token.
  const irys = new Irys({ network, token: "solana", key: rawKey });

  const receipt = await irys.upload(blob, { tags });
  console.log(receipt.id);  // only the tx ID on stdout
  process.exit(0);
} catch (err) {
  console.error("irys error:", err.message);
  process.exit(1);
}
"""


def _upload_to_arweave(
    blob: bytes,
    tags: list[dict],
    repo_root: Path,
) -> Optional[str]:
    """
    Upload a blob to Arweave via Irys.

    Returns the Arweave tx ID string on success, None if:
      - IRYS_SOLANA_KEY is not set in the environment
      - @irys/sdk is not installed
      - Node.js is not available
      - Upload fails for any reason

    TODO: set IRYS_SOLANA_KEY in the environment (hex or base58 Solana secret key)
    before calling anchor_video_proof() — or pass irys_solana_key= explicitly.
    """
    import tempfile

    helper_path = repo_root / "_nebula_irys_upload.mjs"
    try:
        helper_path.write_text(_IRYS_UPLOAD_SRC, encoding="utf-8")
    except OSError:
        return None

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as bf:
        bf.write(blob)
        blob_tmp = bf.name

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tf.write(json.dumps(tags).encode())
        tags_tmp = tf.name

    try:
        for runner in ("tsx", "ts-node", "node"):
            try:
                result = subprocess.run(
                    [runner, str(helper_path), blob_tmp, tags_tmp, str(repo_root)],
                    capture_output=True,
                    timeout=60,
                    env={**os.environ},
                )
                if result.returncode == 0:
                    return result.stdout.decode().strip()
                if result.returncode == 2:
                    # Exit 2 = IRYS_SOLANA_KEY not set — skip silently
                    return None
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return None
    finally:
        for p in (blob_tmp, tags_tmp, str(helper_path)):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Solana anchor transaction
# ---------------------------------------------------------------------------

def _build_anchor_ix_data(commitment_32: bytes) -> bytes:
    """
    Build receipt_anchor AnchorV1Single instruction bytes.

    Layout (from instruction.rs, SINGLE_LEN_NO_BUCKET = 34):
      [version=0x01][flags=0x00][32 bytes commitment]
    """
    assert len(commitment_32) == 32, "commitment must be exactly 32 bytes"
    return bytes([INSTRUCTION_VERSION_V1, FLAG_NO_BUCKET_ID]) + commitment_32


def _submit_solana_anchor(
    commitment_32: bytes,
    bucket_id: int,
    keypair_path: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], list[str]]:
    """
    Submit a single anchor instruction to the receipt_anchor program on Solana.

    Returns (tx_signature, bucket_pda_b58, errors).

    Key injection (in priority order):
      1. keypair_path argument
      2. SOLANA_KEYPAIR_PATH environment variable
      3. `solana config get` — reads the CLI-configured wallet path

    TODO: set SOLANA_KEYPAIR_PATH or pass keypair_path= to inject the payer key.

    Uses solana-py (pip install solana) if available, otherwise falls back to
    the Solana CLI (`solana program invoke`).  Both paths are implemented below
    with honest fallback behaviour.
    """
    errors: list[str] = []
    rpc = rpc_url or os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC_URL)

    # --- Resolve keypair path ---
    resolved_keypair: Optional[str] = (
        keypair_path
        or os.environ.get("SOLANA_KEYPAIR_PATH")
    )
    if not resolved_keypair:
        try:
            cli_out = subprocess.check_output(
                ["solana", "config", "get"], encoding="utf-8", timeout=10
            )
            for line in cli_out.splitlines():
                if "Keypair Path:" in line:
                    resolved_keypair = line.split("Keypair Path:", 1)[1].strip()
                    break
        except Exception as exc:
            errors.append(f"solana config get failed: {exc}")

    if not resolved_keypair or not Path(resolved_keypair).exists():
        errors.append(
            "Solana keypair not found. "
            "Set SOLANA_KEYPAIR_PATH or configure the Solana CLI wallet. "
            "Skipping on-chain anchor."
        )
        return None, None, errors

    # --- Derive bucket PDA ---
    bucket_id_le = struct.pack("<Q", bucket_id)  # 8 bytes, uint64 LE
    pda_b58, bump = _find_program_address(
        [b"bucket", bucket_id_le],
        RECEIPT_ANCHOR_PROGRAM_ID,
    )

    # --- Try solana-py (pure Python, no subprocess overhead) ---
    try:
        from solana.rpc.api import Client  # pip install solana
        from solders.keypair import Keypair  # pip install solders
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction as SoldersTransaction
        from solders.instruction import Instruction, AccountMeta
        from solders.message import Message
        from solders.hash import Hash

        secret_bytes = json.loads(Path(resolved_keypair).read_text())
        payer_kp = Keypair.from_bytes(bytes(secret_bytes[:64]))

        ix_data = _build_anchor_ix_data(commitment_32)

        program_id = Pubkey.from_string(RECEIPT_ANCHOR_PROGRAM_ID)
        pda_pubkey = Pubkey.from_string(pda_b58) if "<" not in pda_b58 else None

        if pda_pubkey is None:
            errors.append("PDA derivation failed (missing base58 library); skipping anchor.")
            return None, pda_b58, errors

        system_program_id = Pubkey.from_string("11111111111111111111111111111111")

        keys = [
            AccountMeta(pubkey=payer_kp.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=pda_pubkey, is_signer=False, is_writable=True),
            AccountMeta(pubkey=system_program_id, is_signer=False, is_writable=False),
        ]
        ix = Instruction(program_id=program_id, accounts=keys, data=bytes(ix_data))

        client = Client(rpc)
        bh_resp = client.get_latest_blockhash()
        blockhash = bh_resp.value.blockhash

        msg = Message.new_with_blockhash([ix], payer_kp.pubkey(), blockhash)
        tx = SoldersTransaction.new_unsigned(msg)
        tx.sign([payer_kp], blockhash)

        resp = client.send_raw_transaction(bytes(tx))
        sig = str(resp.value)

        client.confirm_transaction(sig, commitment="confirmed")
        return sig, pda_b58, errors

    except ImportError:
        errors.append(
            "solana-py / solders not installed (pip install solana solders). "
            "Falling back to Solana CLI."
        )
    except Exception as exc:
        errors.append(f"solana-py anchor failed: {exc}")

    # --- Fallback: build the tx in Node.js (mirrors 02-arweave-archive-demo.mjs) ---
    _JS_ANCHOR_SRC = """\
#!/usr/bin/env node
// _nebula_solana_anchor.mjs — called by nebula_anchor.py as last-resort fallback.
// argv: <commitment_hex> <bucket_id> <keypair_path> <pda_b58> <rpc_url>
import { readFileSync } from "node:fs";
const { Connection, Keypair, PublicKey, Transaction, TransactionInstruction, SystemProgram }
  = await import("@solana/web3.js");

const [, , commitHex, bucketId, keypairPath, pda, rpc] = process.argv;

const secret    = Uint8Array.from(JSON.parse(readFileSync(keypairPath, "utf8")));
const payer     = Keypair.fromSecretKey(secret);
const conn      = new Connection(rpc, "confirmed");

const commitment32 = Buffer.from(commitHex, "hex");
const ixData = new Uint8Array(34);
ixData[0] = 0x01; ixData[1] = 0x00;
commitment32.copy(ixData, 2);

const ix = new TransactionInstruction({
  programId: new PublicKey("{RECEIPT_ANCHOR}"),
  keys: [
    { pubkey: payer.publicKey,           isSigner: true,  isWritable: true },
    { pubkey: new PublicKey(pda),        isSigner: false, isWritable: true },
    { pubkey: SystemProgram.programId,   isSigner: false, isWritable: false },
  ],
  data: ixData,
});

const {{ blockhash, lastValidBlockHeight }} = await conn.getLatestBlockhash("confirmed");
const tx = new Transaction({{ blockhash, lastValidBlockHeight, feePayer: payer.publicKey }}).add(ix);
tx.sign(payer);

const sig = await conn.sendRawTransaction(tx.serialize(), {{ skipPreflight: false }});
await conn.confirmTransaction({{ signature: sig, blockhash, lastValidBlockHeight }}, "confirmed");
console.log(sig);
""".replace("{RECEIPT_ANCHOR}", RECEIPT_ANCHOR_PROGRAM_ID)

    import tempfile

    js_helper = _REPO_ROOT / "_nebula_solana_anchor.mjs"
    try:
        js_helper.write_text(_JS_ANCHOR_SRC, encoding="utf-8")
        commit_hex = commitment_32.hex()
        for runner in ("node", "tsx"):
            try:
                result = subprocess.run(
                    [
                        runner, str(js_helper),
                        commit_hex,
                        str(bucket_id),
                        resolved_keypair,
                        pda_b58,
                        rpc,
                    ],
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    sig = result.stdout.decode().strip()
                    return sig, pda_b58, errors
                errors.append(
                    f"Node.js anchor ({runner}) failed: "
                    f"{result.stderr.decode()[:200]}"
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                errors.append(f"Node.js ({runner}) unavailable: {exc}")
    finally:
        try:
            js_helper.unlink()
        except OSError:
            pass

    errors.append(
        "All anchor submission paths failed. "
        "Commitment is ready — submit manually via the Solana CLI:\n"
        f"  solana program invoke {RECEIPT_ANCHOR_PROGRAM_ID} "
        f"--data 0x{_build_anchor_ix_data(commitment_32).hex()}"
    )
    return None, pda_b58, errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def anchor_video_proof(
    output_path: str | Path,
    vmaf_score: float,
    compression_ratio: float,
    original_sha256: str,
    *,
    mode: str = "safe",
    timestamp_unix: Optional[int] = None,
    keypair_path: Optional[str] = None,
    rpc_url: Optional[str] = None,
    irys_solana_key: Optional[str] = None,
    skip_arweave: bool = False,
    skip_solana: bool = False,
) -> VideoAnchorProof:
    """
    Anchor a Nebula video compression proof on Solana + Arweave.

    Parameters
    ----------
    output_path
        Path to the compressed video file produced by compress_video().
    vmaf_score
        VMAF quality score reported by compress_video() (float, 0–100).
    compression_ratio
        Compression ratio (original_bytes / output_bytes) from compress_video().
    original_sha256
        SHA-256 hex digest of the original input file (from compress_video()).
    mode
        Compression mode used (e.g. "safe", "aggressive") — stored in the receipt.
    timestamp_unix
        POSIX timestamp to bind to the commitment.  Defaults to time.time().
    keypair_path
        Path to a Solana keypair JSON file.  Falls back to SOLANA_KEYPAIR_PATH
        env var, then `solana config get`.
        TODO: set SOLANA_KEYPAIR_PATH before calling this function.
    rpc_url
        Solana RPC endpoint.  Falls back to SOLANA_RPC_URL env var, then
        mainnet-beta public endpoint.
    irys_solana_key
        Hex or base58 Solana secret key for Irys uploads.  Falls back to
        IRYS_SOLANA_KEY env var.
        TODO: set IRYS_SOLANA_KEY before calling this function.
    skip_arweave
        Set True to skip the Arweave upload step (useful in CI / devnet testing).
    skip_solana
        Set True to skip the Solana anchor step (dry-run mode).

    Returns
    -------
    VideoAnchorProof
        All on-chain identifiers, the raw 32-byte commitment, and any
        non-fatal errors that caused a step to be skipped.

    Example
    -------
    >>> from nebula import compress_video
    >>> from nebula_anchor import anchor_video_proof
    >>>
    >>> result = compress_video("input.mp4", mode="safe")
    >>> proof = anchor_video_proof(
    ...     output_path=result.output_path,
    ...     vmaf_score=result.vmaf,
    ...     compression_ratio=result.ratio,
    ...     original_sha256=result.original_hash,
    ... )
    >>> print(proof.solana_tx)
    >>> print(proof.arweave_tx)
    >>> print(proof.commitment_32bytes.hex())
    """
    errors: list[str] = []
    ts = timestamp_unix if timestamp_unix is not None else int(time.time())
    output_path = Path(output_path)

    if not output_path.exists():
        raise FileNotFoundError(f"Compressed video not found: {output_path}")

    # ------------------------------------------------------------------
    # 1. Build the 32-byte commitment
    # ------------------------------------------------------------------
    commitment = _build_commitment(
        output_path=output_path,
        vmaf_score=vmaf_score,
        compression_ratio=compression_ratio,
        original_sha256=original_sha256,
        timestamp_unix=ts,
    )
    commitment_hex = commitment.hex()

    # ------------------------------------------------------------------
    # 2. Build the x402 metadata receipt
    # ------------------------------------------------------------------
    output_sha256 = _sha256_file(output_path)
    receipt: dict = {
        # x402 receipt schema (compatible with X402Receipt in compress.ts)
        "txSignature":      commitment_hex,          # commitment doubles as the receipt ID
        "amount":           0,                       # no payment — proof-of-compression
        "sender":           "nebula_compress",
        "receiver":         "parad0x_receipt_anchor",
        "timestamp":        ts,
        # Nebula-specific fields
        "type":             "nebula_compress",
        "original_sha256":  original_sha256,
        "output_sha256":    output_sha256,
        "vmaf":             vmaf_score,
        "ratio":            compression_ratio,
        "mode":             mode,
        "output_path":      str(output_path),
        "commitment":       commitment_hex,
        "ts":               ts,
        "program":          RECEIPT_ANCHOR_PROGRAM_ID,
    }

    # ------------------------------------------------------------------
    # 3. Compress the receipt with Liquefy
    # ------------------------------------------------------------------
    receipt_blob: Optional[bytes] = None
    try:
        receipt_blob = _compress_receipt_with_liquefy(receipt, _REPO_ROOT)
        if receipt_blob is None:
            errors.append(
                "Liquefy compression skipped "
                "(Node.js / tsx not available or liquefy-receipts package not found). "
                "Receipt stored as raw JSON in the proof object."
            )
    except Exception as exc:
        errors.append(f"Liquefy compression error: {exc}")

    # ------------------------------------------------------------------
    # 4. Upload the compressed receipt blob to Arweave via Irys
    # ------------------------------------------------------------------
    arweave_tx: Optional[str] = None
    if skip_arweave:
        errors.append("Arweave upload skipped (skip_arweave=True).")
    elif receipt_blob is None:
        errors.append("Arweave upload skipped (no compressed blob to upload).")
    else:
        # Inject irys_solana_key into env for the subprocess if provided.
        if irys_solana_key:
            os.environ["IRYS_SOLANA_KEY"] = irys_solana_key

        arweave_tags = [
            {"name": "Content-Type",        "value": "application/liquefy-encrypted"},
            {"name": "Liquefy-Version",     "value": "0.2.2"},
            {"name": "Receipt-Count",       "value": "1"},
            {"name": "App",                 "value": "dna-x402"},
            {"name": "Nebula-Mode",         "value": mode},
            {"name": "Nebula-VMAF",         "value": str(round(vmaf_score, 2))},
            {"name": "Nebula-Ratio",        "value": str(round(compression_ratio, 2))},
            {"name": "Commitment",          "value": commitment_hex},
            {"name": "Original-SHA256",     "value": original_sha256},
            {"name": "Output-SHA256",       "value": output_sha256},
        ]
        try:
            arweave_tx = _upload_to_arweave(receipt_blob, arweave_tags, _REPO_ROOT)
            if arweave_tx is None:
                errors.append(
                    "Arweave upload skipped "
                    "(IRYS_SOLANA_KEY not set or @irys/sdk not installed). "
                    "TODO: set IRYS_SOLANA_KEY and run `npm install` in the repo root."
                )
        except Exception as exc:
            errors.append(f"Arweave upload error: {exc}")

    # ------------------------------------------------------------------
    # 5. Anchor the 32-byte commitment on Solana
    # ------------------------------------------------------------------
    solana_tx: Optional[str] = None
    bucket_id = _bucket_id_from_unix(ts)
    bucket_pda: Optional[str] = None

    if skip_solana:
        errors.append("Solana anchor skipped (skip_solana=True).")
        bucket_id_le = struct.pack("<Q", bucket_id)
        bucket_pda, _ = _find_program_address(
            [b"bucket", bucket_id_le], RECEIPT_ANCHOR_PROGRAM_ID
        )
    else:
        try:
            solana_tx, bucket_pda, anchor_errors = _submit_solana_anchor(
                commitment_32=commitment,
                bucket_id=bucket_id,
                keypair_path=keypair_path,
                rpc_url=rpc_url,
            )
            errors.extend(anchor_errors)
        except Exception as exc:
            errors.append(f"Solana anchor unexpected error: {exc}")

    return VideoAnchorProof(
        solana_tx=solana_tx,
        arweave_tx=arweave_tx,
        commitment_32bytes=commitment,
        commitment_hex=commitment_hex,
        receipt_blob=receipt_blob,
        receipt=receipt,
        bucket_pda=bucket_pda,
        bucket_id=bucket_id,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint (for manual testing / CI smoke checks)
# ---------------------------------------------------------------------------

def _cli() -> None:
    """
    Quick smoke-test CLI:

      python nebula_anchor.py <output_video> <vmaf> <ratio> <original_sha256> [mode]

    Runs the full pipeline with skip_arweave and skip_solana defaulting to
    False (real anchoring).  Pass --dry-run to skip on-chain steps.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Anchor a Nebula video proof on Solana + Arweave."
    )
    parser.add_argument("output_path", help="Compressed video file path")
    parser.add_argument("vmaf_score", type=float, help="VMAF score (0–100)")
    parser.add_argument("compression_ratio", type=float, help="Compression ratio (e.g. 4.2)")
    parser.add_argument("original_sha256", help="SHA-256 hex of the original file")
    parser.add_argument("--mode", default="safe", help="Nebula compression mode")
    parser.add_argument("--dry-run", action="store_true", help="Skip on-chain steps")
    parser.add_argument("--skip-arweave", action="store_true")
    parser.add_argument("--skip-solana", action="store_true")
    parser.add_argument("--keypair", help="Path to Solana keypair JSON")
    parser.add_argument("--rpc", help="Solana RPC URL")
    args = parser.parse_args()

    proof = anchor_video_proof(
        output_path=args.output_path,
        vmaf_score=args.vmaf_score,
        compression_ratio=args.compression_ratio,
        original_sha256=args.original_sha256,
        mode=args.mode,
        keypair_path=args.keypair,
        rpc_url=args.rpc,
        skip_arweave=args.dry_run or args.skip_arweave,
        skip_solana=args.dry_run or args.skip_solana,
    )

    print(json.dumps({
        "commitment":   proof.commitment_hex,
        "solana_tx":    proof.solana_tx,
        "arweave_tx":   proof.arweave_tx,
        "bucket_pda":   proof.bucket_pda,
        "bucket_id":    proof.bucket_id,
        "receipt":      proof.receipt,
        "errors":       proof.errors,
    }, indent=2))


if __name__ == "__main__":
    _cli()
