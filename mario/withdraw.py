"""Withdraw SOL from Mario's trading wallet - only to an address YOU allow-listed.

Directive §9: "No ability to withdraw funds to an unknown address." So a withdrawal
only goes to an address in TREASURY_ADDRESS (in .env, comma-separated if several).
The trading engine never calls this; only you can, from the command line, and you
must type a confirmation.
"""
from __future__ import annotations

import asyncio
import os
import struct

from .clients.rpc import LAMPORTS, SolanaRPC
from .solana_tx import SYSTEM_PROGRAM, b64, build_tx, parse_transaction, sign_transaction
from .util import b58decode, is_pubkey

FEE_LAMPORTS = 5000


class WithdrawError(Exception):
    pass


def allowed_addresses() -> list[str]:
    return [a.strip() for a in os.environ.get("TREASURY_ADDRESS", "").split(",") if a.strip()]


def check_destination(to: str, own: str) -> None:
    if not is_pubkey(to):
        raise WithdrawError(f"{to!r} is not a valid Solana address")
    if to == own:
        raise WithdrawError("that is the trading wallet itself")
    allow = allowed_addresses()
    if not allow:
        raise WithdrawError("no TREASURY_ADDRESS set. Add your own wallet address to .env first:\n"
                            "  TREASURY_ADDRESS=YourPhantomAddress")
    if to not in allow:
        raise WithdrawError(f"{to} is not in TREASURY_ADDRESS. Mario only withdraws to addresses you listed there.")


def build_sol_transfer(kp, to: str, lamports: int, blockhash: str) -> bytes:
    """Legacy tx: one System Program transfer, signed by the trading wallet."""
    keys = [kp.pubkey_bytes, b58decode(to), b58decode(SYSTEM_PROGRAM)]
    data = struct.pack("<IQ", 2, lamports)
    raw = build_tx(keys, 1, b58decode(blockhash), [(2, [0, 1], data)], v0=False, ro_unsigned=1)
    return sign_transaction(raw, kp.pubkey, kp.sign)


async def withdraw_sol(rpc: SolanaRPC, kp, to: str, amount_sol: float | None) -> tuple[str, float]:
    """amount_sol=None means everything minus the network fee. Returns (signature, sol_sent)."""
    check_destination(to, kp.pubkey)
    bal = await rpc.get_balance(kp.pubkey)
    lamports = bal - FEE_LAMPORTS if amount_sol is None else int(round(amount_sol * LAMPORTS))
    if lamports <= 0:
        raise WithdrawError(f"balance {bal / LAMPORTS:.6f} SOL is too small to withdraw")
    if lamports + FEE_LAMPORTS > bal:
        raise WithdrawError(f"not enough SOL: balance {bal / LAMPORTS:.6f}, requested {lamports / LAMPORTS:.6f} + fee")
    bh = (await rpc.call("getLatestBlockhash", [{"commitment": "finalized"}]))["value"]["blockhash"]
    tx = build_sol_transfer(kp, to, lamports, bh)
    parsed = parse_transaction(tx)            # self-check before sending
    assert parsed.static_keys[1] == to and len(parsed.instructions) == 1
    sig = await rpc.send(b64(tx))
    for _ in range(40):
        st = await rpc.signature_status(sig)
        if st:
            if st.get("err"):
                raise WithdrawError(f"transaction failed on-chain: {st['err']} (signature {sig})")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig, lamports / LAMPORTS
        await asyncio.sleep(1.5)
    raise WithdrawError(f"sent but not confirmed yet - check https://solscan.io/tx/{sig}")
