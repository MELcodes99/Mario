"""Position monitoring and deterministic exits (directive §7).

Prices are *executable sell quotes* for the tokens actually held, never the
last-trade price. Emergency exits never wait for the AI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from .config import Settings
from .execution import ExecutionRejected
from .journal import Journal
from .risk import RiskEngine

log = logging.getLogger("mario.positions")


@dataclass
class Position:
    id: int
    mint: str
    symbol: str
    strategy: str
    creator: str | None
    opened_ts: float
    sol_in: float
    tokens_total: int
    tokens_left: int
    realized_sol: float
    fees_sol: float
    stop_ratio: float
    peak_ratio: float
    mae: float
    tp_hits: list
    entry_liq: float
    entry_mc: float
    quote_failures: int = 0
    last_value_sol: float | None = None
    last_auth_check: float = 0.0

    @property
    def basis_left(self) -> float:
        return self.sol_in * self.tokens_left / self.tokens_total if self.tokens_total else 0.0


class PositionManager:
    def __init__(self, s: Settings, j: Journal, executor, risk: RiskEngine, notify=None):
        self.s, self.j, self.ex, self.risk = s, j, executor, risk
        self.notify = notify
        self.open: dict[int, Position] = {}
        self.lock = asyncio.Lock()       # monitor and close_all never sell the same tokens twice
        self.load()

    def load(self):
        for r in self.j.q("SELECT * FROM positions WHERE status='open' AND mode=?", (self.ex.mode,)):
            self.open[r["id"]] = Position(r["id"], r["mint"], r["symbol"], r["strategy"], r["creator"],
                                          r["opened_ts"], r["sol_in"], r["tokens_total"], r["tokens_left"],
                                          r["realized_sol"], r["fees_sol"], r["stop_ratio"], r["peak_ratio"],
                                          r["mae"] or 1.0, json.loads(r["tp_hits"] or "[]"), r["entry_liq"] or 0,
                                          r["entry_mc"] or 0)

    # ---- accounting
    def exposure_sol(self) -> float:
        return sum(p.last_value_sol if p.last_value_sol is not None else p.basis_left for p in self.open.values())

    def holds(self, mint: str) -> bool:
        return any(p.mint == mint for p in self.open.values())

    def _persist(self, p: Position, status="open", reason=None):
        self.j.x("""UPDATE positions SET tokens_left=?, realized_sol=?, fees_sol=?, stop_ratio=?, peak_ratio=?,
                    mae=?, mfe=?, tp_hits=?, status=?, exit_reason=COALESCE(?, exit_reason),
                    closed_ts=CASE WHEN ?='closed' THEN ? ELSE closed_ts END WHERE id=?""",
                 (p.tokens_left, p.realized_sol, p.fees_sol, p.stop_ratio, p.peak_ratio, p.mae, p.peak_ratio,
                  json.dumps(p.tp_hits), status, reason, status, time.time(), p.id))

    def open_position(self, fill, pair, strategy, score, creator, session_id, entry_reason, slippage_pct) -> Position:
        stop_ratio = 1 - self.s.stop_loss_pct / 100
        pid = self.j.x("""INSERT INTO positions(mode,session_id,mint,symbol,strategy,creator,opened_ts,sol_in,
                tokens_total,tokens_left,decimals,realized_sol,fees_sol,stop_ratio,peak_ratio,tp_hits,entry_liq,
                entry_mc,score,status,mae,mfe,entry_reason,slippage_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (self.ex.mode, session_id, pair.mint, pair.symbol, strategy, creator, time.time(), fill.sol,
                        fill.tokens, fill.tokens, 0, 0.0, fill.fees_sol, stop_ratio, 1.0, "[]", pair.liquidity_usd,
                        pair.mc, score, "open", 1.0, 1.0, entry_reason,
                        json.dumps({"impact": fill.price_impact_pct, "max_slippage": slippage_pct})))
        self.j.x("INSERT INTO fills(position_id,ts,side,sol,tokens,price_impact,signature,reason,mode) VALUES(?,?,?,?,?,?,?,?,?)",
                 (pid, time.time(), "buy", fill.sol, fill.tokens, fill.price_impact_pct, fill.signature, entry_reason,
                  self.ex.mode))
        p = Position(pid, pair.mint, pair.symbol, strategy, creator, time.time(), fill.sol, fill.tokens, fill.tokens,
                     0.0, fill.fees_sol, stop_ratio, 1.0, 1.0, [], pair.liquidity_usd, pair.mc)
        self.open[pid] = p
        return p

    async def _sell(self, p: Position, tokens: int, reason: str, emergency=False) -> bool:
        tokens = min(tokens, p.tokens_left)
        if tokens <= 0:
            return False
        slip = self.s.emergency_exit_max_slippage_pct if emergency else self.s.exit_max_slippage_pct
        try:
            f = await self.ex.sell(p.mint, tokens, slip, emergency=emergency)
        except (ExecutionRejected, ValueError) as e:
            log.warning("[%s] sell (%s) failed: %s", p.symbol, reason, e)
            p.quote_failures += 1
            return False
        p.tokens_left -= f.tokens
        p.realized_sol += f.sol
        p.fees_sol += f.fees_sol
        self.j.x("INSERT INTO fills(position_id,ts,side,sol,tokens,price_impact,signature,reason,mode) VALUES(?,?,?,?,?,?,?,?,?)",
                 (p.id, time.time(), "sell", f.sol, f.tokens, f.price_impact_pct, f.signature, reason, self.ex.mode))
        log.info("[%s] SELL %s tokens for %.4f SOL (%s)", p.symbol, f.tokens, f.sol, reason)
        if p.tokens_left <= max(1, p.tokens_total // 1000):
            await self._close(p, reason)
        else:
            self._persist(p)
        return True

    async def _close(self, p: Position, reason: str):
        pnl = p.realized_sol - p.sol_in - p.fees_sol
        self._persist(p, "closed", reason)
        self.open.pop(p.id, None)
        self.risk.record_close(pnl)
        msg = (f"{'🟢' if pnl >= 0 else '🔴'} CLOSED {p.symbol} [{self.ex.mode}] {reason}: "
               f"in {p.sol_in:.4f} SOL, out {p.realized_sol:.4f} SOL, PnL {pnl:+.4f} SOL "
               f"({100 * pnl / p.sol_in:+.1f}%)")
        log.info(msg)
        if self.notify:
            await self.notify(msg)
        if reason.startswith("EMERGENCY") and p.creator:
            self.j.blacklist(p.creator, "deployer", f"emergency exit on {p.mint}: {reason}")
            self.j.blacklist(p.mint, "mint", reason)

    async def close_all(self, reason: str):
        async with self.lock:
            await self._close_all(reason)

    async def _close_all(self, reason: str):
        for p in list(self.open.values()):
            await self._sell(p, p.tokens_left, reason, emergency=reason.startswith("EMERGENCY"))

    # ---- one monitoring pass
    async def monitor(self, pairs: dict, emergency_check=None):
        async with self.lock:
            await self._monitor(pairs, emergency_check)

    async def _monitor(self, pairs: dict, emergency_check=None):
        for p in list(self.open.values()):
            pair = pairs.get(p.mint)
            # 1. emergency conditions (deterministic, first)
            emergency = None
            if pair and p.entry_liq and pair.liquidity_usd < p.entry_liq * (1 - self.s.emergency_liq_drop_pct / 100):
                emergency = f"liquidity dropped {100 * (1 - pair.liquidity_usd / p.entry_liq):.0f}%"
            if emergency is None and emergency_check and time.time() - p.last_auth_check > self.s.authority_recheck_s:
                p.last_auth_check = time.time()
                emergency = await emergency_check(p)
            if emergency:
                log.warning("[%s] EMERGENCY: %s", p.symbol, emergency)
                await self._sell(p, p.tokens_left, f"EMERGENCY {emergency}", emergency=True)
                continue
            # 2. executable value of what we hold
            try:
                q = await self.ex.quote_sell(p.mint, p.tokens_left)
                value = q.out_amount / 1e9
                p.quote_failures = 0
            except Exception as e:
                p.quote_failures += 1
                log.warning("[%s] sell quote failed (%s/%s): %s", p.symbol, p.quote_failures,
                            self.s.max_quote_failures, e)
                if p.quote_failures >= self.s.max_quote_failures:
                    await self._sell(p, p.tokens_left, "EMERGENCY sell route failing", emergency=True)
                continue
            p.last_value_sol = value
            ratio = value / p.basis_left if p.basis_left else 0
            p.peak_ratio = max(p.peak_ratio, ratio)
            p.mae = min(p.mae, ratio)
            # 3. stop-loss (moves up only)
            if ratio <= p.stop_ratio:
                kind = ("trailing stop" if len(p.tp_hits) >= len(self.s.tp_levels)
                        else "breakeven stop" if p.stop_ratio >= 1.0 else "stop-loss")
                await self._sell(p, p.tokens_left, f"{kind} {100 * (ratio - 1):+.1f}% (peak {100 * (p.peak_ratio - 1):+.0f}%)")
                continue
            # 4. take-profit ladder
            sold = False
            for i, lvl in enumerate(self.s.tp_levels):
                if i not in p.tp_hits and ratio >= 1 + lvl / 100:
                    chunk = int(p.tokens_total * self.s.tp_fraction)
                    if await self._sell(p, chunk, f"take-profit +{lvl:.0f}%"):
                        p.tp_hits.append(i)
                        if i == 0 and self.s.breakeven_after_tp1:
                            p.stop_ratio = max(p.stop_ratio, 1.0)
                        self._persist(p) if p.id in self.open else None
                        sold = True
                    break
            if sold or p.id not in self.open:
                continue
            # 5. trailing stop on the remainder once the ladder is done
            if len(p.tp_hits) >= len(self.s.tp_levels):
                weak = pair is not None and pair.chg("m5") < 0 and pair.sells("m5") > pair.buys("m5")
                d = (self.s.trailing_tight_pct if weak else self.s.trailing_pct) / 100
                trail = p.peak_ratio * (1 - d)
                p.stop_ratio = max(p.stop_ratio, trail)   # never loosens
                if ratio <= p.stop_ratio:
                    await self._sell(p, p.tokens_left, f"trailing stop (peak {100 * (p.peak_ratio - 1):+.0f}%)")
                    continue
            self._persist(p)
