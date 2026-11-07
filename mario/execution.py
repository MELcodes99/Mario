"""Execution engine (directive §9). Paper and live share the exact same pre-trade
checks; live additionally validates instructions, simulates, signs, submits and
confirms. A malicious token, a compromised AI response or a manipulated quote
cannot make Mario sign an unvalidated transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .clients.jupiter import SOL_MINT, Jupiter, Quote
from .clients.rpc import LAMPORTS, SolanaRPC
from .config import Settings
from .journal import Journal
from .solana_tx import (DEFAULT_ALLOWED_PROGRAMS, b64, parse_transaction, sign_transaction, unb64,
                        validate_swap_tx)

log = logging.getLogger("mario.exec")

ATA_RENT_SOL = 0.0021


class ExecutionRejected(Exception):
    pass


@dataclass
class Fill:
    side: str
    sol: float               # SOL spent (buy) or received (sell)
    tokens: int              # raw token units received (buy) or sold (sell)
    price_impact_pct: float
    fees_sol: float
    signature: str | None


def precheck(q: Quote, max_slippage_pct: float, max_impact_pct: float):
    if q.age_s > 10:
        raise ExecutionRejected("quote is stale")
    if q.price_impact_pct > max_impact_pct:
        raise ExecutionRejected(f"price impact {q.price_impact_pct:.2f}% > {max_impact_pct}%")
    if q.implied_slippage_pct > max_slippage_pct:
        raise ExecutionRejected(f"route slippage {q.implied_slippage_pct:.2f}% > {max_slippage_pct}%")


class PaperExecutor:
    mode = "paper"

    def __init__(self, s: Settings, jup: Jupiter, j: Journal):
        self.s, self.jup, self.j = s, jup, j
        if self.j.get("paper_cash") is None:
            self.j.put("paper_cash", s.paper_start_sol)

    async def cash_sol(self) -> float:
        return float(self.j.get("paper_cash", 0.0))

    async def quote_buy(self, mint: str, sol: float) -> Quote:
        return await self.jup.order(SOL_MINT, mint, int(sol * LAMPORTS))

    async def quote_sell(self, mint: str, tokens: int) -> Quote:
        return await self.jup.order(mint, SOL_MINT, int(tokens))

    async def buy(self, mint: str, sol: float, max_slippage_pct: float) -> Fill:
        if sol + self.s.network_fee_sol > await self.cash_sol():
            raise ExecutionRejected("insufficient paper balance")
        q = await self.quote_buy(mint, sol)
        precheck(q, max_slippage_pct, self.s.max_price_impact_pct)
        tokens = int(q.out_amount * 0.995)       # latency haircut: paper should not flatter results
        fee = self.s.network_fee_sol + ATA_RENT_SOL * 0  # rent is refundable; ignored
        self.j.put("paper_cash", await self.cash_sol() - sol - fee)
        return Fill("buy", sol, tokens, q.price_impact_pct, fee, None)

    async def sell(self, mint: str, tokens: int, max_slippage_pct: float, emergency: bool = False) -> Fill:
        q = await self.quote_sell(mint, tokens)
        impact_cap = 100.0 if emergency else max(self.s.max_price_impact_pct, max_slippage_pct)
        precheck(q, max_slippage_pct, impact_cap)
        sol = q.out_amount * 0.995 / LAMPORTS
        fee = self.s.network_fee_sol
        self.j.put("paper_cash", await self.cash_sol() + sol - fee)
        return Fill("sell", sol, tokens, q.price_impact_pct, fee, None)


class LiveExecutor:
    mode = "live"

    def __init__(self, s: Settings, jup: Jupiter, rpc: SolanaRPC, keypair, j: Journal):
        self.s, self.jup, self.rpc, self.kp, self.j = s, jup, rpc, keypair, j
        self.wallet = keypair.pubkey
        extra = {p.strip() for p in s.extra_allowed_programs.split(",") if p.strip()}
        self.allowed = DEFAULT_ALLOWED_PROGRAMS | extra

    async def cash_sol(self) -> float:
        return await self.rpc.get_balance(self.wallet) / LAMPORTS

    async def quote_buy(self, mint: str, sol: float) -> Quote:
        return await self.jup.order(SOL_MINT, mint, int(sol * LAMPORTS))

    async def quote_sell(self, mint: str, tokens: int) -> Quote:
        return await self.jup.order(mint, SOL_MINT, int(tokens))

    async def _validated_sim(self, q: Quote) -> tuple[bytes, int, int]:
        if not q.transaction or not q.request_id:
            raise ExecutionRejected("router returned no transaction")
        raw = unb64(q.transaction)
        tx = parse_transaction(raw)
        v = validate_swap_tx(tx, self.wallet, self.allowed, int(self.s.max_extra_sol_transfer * LAMPORTS))
        if not v.ok:
            raise ExecutionRejected("instruction validation failed: " + "; ".join(v.problems))
        pre = await self.rpc.get_balance(self.wallet)
        sim = await self.rpc.simulate(q.transaction, [self.wallet])
        if sim.get("err"):
            logs = " | ".join((sim.get("logs") or [])[-4:])
            raise ExecutionRejected(f"simulation failed: {sim['err']} {logs}")
        accs = sim.get("accounts") or []
        if not accs or accs[0] is None:
            raise ExecutionRejected("simulation did not return wallet state")
        post = int(accs[0]["lamports"])
        return raw, pre, post

    async def _submit(self, raw: bytes, q: Quote) -> dict:
        signed = sign_transaction(raw, self.wallet, self.kp.sign)
        res = await self.jup.execute(b64(signed), q.request_id)
        if str(res.get("status", "")).lower() != "success":
            raise ExecutionRejected(f"execution failed: {res.get('error') or res.get('code') or res}")
        sig = res.get("signature")
        if sig:
            st = await self.rpc.signature_status(sig)
            if st and st.get("err"):
                raise ExecutionRejected(f"transaction landed with error {st['err']}")
        return res

    async def buy(self, mint: str, sol: float, max_slippage_pct: float) -> Fill:
        lamports = int(sol * LAMPORTS)
        q = await self.jup.order(SOL_MINT, mint, lamports, taker=self.wallet)
        precheck(q, max_slippage_pct, self.s.max_price_impact_pct)
        if q.in_amount != lamports or q.input_mint != SOL_MINT or q.output_mint != mint:
            raise ExecutionRejected("order does not match the requested trade")
        raw, pre, post = await self._validated_sim(q)
        spent = (pre - post) / LAMPORTS
        max_spend = sol + ATA_RENT_SOL + self.s.network_fee_sol * 4 + self.s.max_extra_sol_transfer
        if spent > max_spend:
            raise ExecutionRejected(f"simulation spends {spent:.5f} SOL > allowed {max_spend:.5f}")
        res = await self._submit(raw, q)
        tokens = int(res.get("outputAmountResult") or 0) or q.out_amount
        sol_in = int(res.get("inputAmountResult") or lamports) / LAMPORTS
        return Fill("buy", sol_in, tokens, q.price_impact_pct, max(0.0, spent - sol), res.get("signature"))

    async def sell(self, mint: str, tokens: int, max_slippage_pct: float, emergency: bool = False) -> Fill:
        held, _ = await self.rpc.get_token_balance(self.wallet, mint)
        tokens = min(int(tokens), held)
        if tokens <= 0:
            raise ExecutionRejected("no tokens to sell")
        q = await self.jup.order(mint, SOL_MINT, tokens, taker=self.wallet)
        impact_cap = 100.0 if emergency else max(self.s.max_price_impact_pct, max_slippage_pct)
        precheck(q, max_slippage_pct, impact_cap)
        if q.input_mint != mint or q.output_mint != SOL_MINT:
            raise ExecutionRejected("order does not match the requested trade")
        raw, pre, post = await self._validated_sim(q)
        if post < pre - int(self.s.network_fee_sol * 4 * LAMPORTS):
            raise ExecutionRejected("simulated sell would reduce SOL balance")
        res = await self._submit(raw, q)
        sol_out = int(res.get("outputAmountResult") or q.out_amount) / LAMPORTS
        return Fill("sell", sol_out, tokens, q.price_impact_pct, self.s.network_fee_sol, res.get("signature"))
