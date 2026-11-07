"""Minimal Solana transaction handling with no Solana SDK dependency.

- parse legacy and v0 transactions
- validate instructions against an allowlist (directive §9: "validate the
  transaction instructions", "no unauthorized transfers", "no arbitrary approvals")
- sign the fee-payer slot with an ed25519 key (cryptography package)
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field

from .util import b58encode, read_compact_u16, write_compact_u16

SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
MEMO_V1 = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"
MEMO_V2 = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

DEFAULT_ALLOWED_PROGRAMS = {
    SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ATA_PROGRAM,
    COMPUTE_BUDGET, MEMO_V1, MEMO_V2, JUPITER_V6,
}

# SPL token instruction tags that must never appear top-level in a swap we sign
TOKEN_FORBIDDEN = {
    4: "Approve", 13: "ApproveChecked", 6: "SetAuthority",
    3: "Transfer", 12: "TransferChecked",   # tokens leaving our wallet outside the router
}
TOKEN_CLOSE_ACCOUNT = 9
TOKEN_SYNC_NATIVE = 17
SYSTEM_TRANSFER = 2
SYSTEM_ASSIGN = 1
SYSTEM_TRANSFER_WITH_SEED = 11


@dataclass
class Instruction:
    program_index: int
    accounts: list[int]
    data: bytes


@dataclass
class ParsedTx:
    raw: bytes
    num_signatures: int
    sig_offset: int
    message_offset: int
    version: str                    # "legacy" or "0"
    num_required_signatures: int
    static_keys: list[str]
    recent_blockhash: str
    instructions: list[Instruction]
    lookup_count: int = 0
    lookups: list = field(default_factory=list)

    @property
    def message(self) -> bytes:
        return self.raw[self.message_offset:]

    def program_id(self, ix: Instruction) -> str:
        if ix.program_index >= len(self.static_keys):
            return f"<lookup:{ix.program_index}>"
        return self.static_keys[ix.program_index]

    def key_at(self, idx: int) -> str | None:
        return self.static_keys[idx] if idx < len(self.static_keys) else None


def parse_transaction(raw: bytes) -> ParsedTx:
    nsig, off = read_compact_u16(raw, 0)
    sig_offset = off
    off += 64 * nsig
    msg_off = off
    first = raw[off]
    if first & 0x80:
        version = str(first & 0x7F)
        if version != "0":
            raise ValueError(f"unsupported tx version {version}")
        off += 1
    else:
        version = "legacy"
    n_req, _ro_signed, _ro_unsigned = raw[off], raw[off + 1], raw[off + 2]
    off += 3
    nkeys, off = read_compact_u16(raw, off)
    keys = []
    for _ in range(nkeys):
        keys.append(b58encode(raw[off:off + 32]))
        off += 32
    blockhash = b58encode(raw[off:off + 32])
    off += 32
    nix, off = read_compact_u16(raw, off)
    ixs = []
    for _ in range(nix):
        prog = raw[off]
        off += 1
        nacc, off = read_compact_u16(raw, off)
        accs = list(raw[off:off + nacc])
        off += nacc
        dlen, off = read_compact_u16(raw, off)
        data = raw[off:off + dlen]
        off += dlen
        ixs.append(Instruction(prog, accs, data))
    lookups = []
    if version == "0":
        nl, off = read_compact_u16(raw, off)
        for _ in range(nl):
            tbl = b58encode(raw[off:off + 32])
            off += 32
            nw, off = read_compact_u16(raw, off)
            w = list(raw[off:off + nw]); off += nw
            nr, off = read_compact_u16(raw, off)
            r = list(raw[off:off + nr]); off += nr
            lookups.append((tbl, w, r))
    if off != len(raw):
        raise ValueError("trailing bytes in transaction")
    return ParsedTx(raw, nsig, sig_offset, msg_off, version, n_req, keys, blockhash, ixs,
                    len(lookups), lookups)


@dataclass
class TxValidation:
    ok: bool
    problems: list[str]


def validate_swap_tx(tx: ParsedTx, wallet: str, allowed_programs: set[str],
                     max_extra_sol_lamports: int) -> TxValidation:
    """Deterministic checks on a router-built swap before we sign it."""
    p: list[str] = []
    if not tx.static_keys or tx.static_keys[0] != wallet:
        p.append("fee payer is not the trading wallet")
    signers = tx.static_keys[:tx.num_required_signatures]
    if wallet not in signers:
        p.append("trading wallet is not a required signer")
    wallet_idx = tx.static_keys.index(wallet) if wallet in tx.static_keys else -1

    # indices of accounts that receive SyncNative (our wrapped-SOL account)
    sync_native_accounts = set()
    for ix in tx.instructions:
        pid = tx.program_id(ix)
        if pid in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) and ix.data[:1] == bytes([TOKEN_SYNC_NATIVE]) and ix.accounts:
            sync_native_accounts.add(ix.accounts[0])

    for n, ix in enumerate(tx.instructions):
        pid = tx.program_id(ix)
        if pid not in allowed_programs:
            p.append(f"ix#{n}: program {pid} is not on the allowlist")
            continue
        if pid == SYSTEM_PROGRAM and len(ix.data) >= 4:
            tag = struct.unpack_from("<I", ix.data, 0)[0]
            if tag == SYSTEM_ASSIGN and ix.accounts and ix.accounts[0] == wallet_idx:
                p.append(f"ix#{n}: tries to re-assign the wallet account owner")
            if tag == SYSTEM_TRANSFER_WITH_SEED:
                p.append(f"ix#{n}: transfer-with-seed not expected in a swap")
            if tag == SYSTEM_TRANSFER and len(ix.data) >= 12 and len(ix.accounts) >= 2:
                lamports = struct.unpack_from("<Q", ix.data, 4)[0]
                src, dst = ix.accounts[0], ix.accounts[1]
                if src == wallet_idx and dst not in sync_native_accounts and lamports > max_extra_sol_lamports:
                    p.append(f"ix#{n}: SOL transfer of {lamports} lamports to a non-wrap account")
        if pid in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) and ix.data:
            tag = ix.data[0]
            if tag in TOKEN_FORBIDDEN:
                p.append(f"ix#{n}: top-level token {TOKEN_FORBIDDEN[tag]} is not allowed")
            if tag == TOKEN_CLOSE_ACCOUNT and len(ix.accounts) >= 2 and ix.accounts[1] != wallet_idx:
                p.append(f"ix#{n}: CloseAccount sends rent to a non-wallet account")
    return TxValidation(not p, p)


def sign_transaction(raw: bytes, wallet_pubkey: str, sign_fn) -> bytes:
    """Write our signature into the slot that belongs to `wallet_pubkey`."""
    tx = parse_transaction(raw)
    signers = tx.static_keys[:tx.num_required_signatures]
    if wallet_pubkey not in signers:
        raise ValueError("wallet is not a signer of this transaction")
    slot = signers.index(wallet_pubkey)
    if slot >= tx.num_signatures:
        raise ValueError("signature slot missing")
    sig = sign_fn(tx.message)
    assert len(sig) == 64
    start = tx.sig_offset + 64 * slot
    return raw[:start] + sig + raw[start + 64:]


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


# helper used by tests to build simple transactions
def build_tx(keys: list[bytes], n_required: int, blockhash: bytes, ixs: list[tuple[int, list[int], bytes]],
             v0: bool = True, ro_signed: int = 0, ro_unsigned: int = 0) -> bytes:
    msg = bytearray()
    if v0:
        msg.append(0x80)
    msg += bytes([n_required, ro_signed, ro_unsigned])
    msg += write_compact_u16(len(keys))
    for k in keys:
        msg += k
    msg += blockhash
    msg += write_compact_u16(len(ixs))
    for prog, accs, data in ixs:
        msg.append(prog)
        msg += write_compact_u16(len(accs)) + bytes(accs)
        msg += write_compact_u16(len(data)) + data
    if v0:
        msg += write_compact_u16(0)
    return write_compact_u16(n_required) + b"\0" * 64 * n_required + bytes(msg)
