"""Market regime (directive §4E) and per-token history Mario records itself."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .clients.dexscreener import DexScreener, Pair

SOL_MINT = "So11111111111111111111111111111111111111112"
CBBTC_MINT = "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"


@dataclass
class Regime:
    sol_price: float
    sol_h1: float
    sol_h24: float
    btc_h24: float | None
    meme_breadth: float | None      # share of watched tokens up over 1h
    fetched_at: float

    @property
    def fresh(self) -> bool:
        return self.sol_price > 0 and time.time() - self.fetched_at < 300

    @property
    def extreme_risk_off(self) -> bool:
        return self.sol_h1 <= -5 or self.sol_h24 <= -12 or (self.btc_h24 is not None and self.btc_h24 <= -10)

    @property
    def label(self) -> str:
        if not self.fresh:
            return "stale"
        if self.extreme_risk_off:
            return "extreme risk-off"
        if self.sol_h1 < -1.5 or self.sol_h24 < -5:
            return "caution"
        return "normal"


class MarketWatch:
    def __init__(self, dex: DexScreener):
        self.dex = dex
        self.regime = Regime(0, 0, 0, None, None, 0)

    async def refresh(self, breadth: float | None = None) -> Regime:
        pairs = await self.dex.tokens([SOL_MINT, CBBTC_MINT])
        sol = pairs.get(SOL_MINT)
        btc = pairs.get(CBBTC_MINT)
        if sol and sol.price_usd > 0:
            self.regime = Regime(sol.price_usd, sol.chg("h1"), sol.chg("h24"),
                                 btc.chg("h24") if btc else None, breadth, time.time())
        return self.regime


class History:
    """Price / liquidity samples per mint, recorded every evaluation pass."""

    def __init__(self, keep_s: int = 6 * 3600):
        self.keep = keep_s
        self.d: dict[str, deque] = {}

    def add(self, p: Pair):
        q = self.d.setdefault(p.mint, deque())
        q.append((time.time(), p.price_usd, p.liquidity_usd, p.mc))
        cut = time.time() - self.keep
        while q and q[0][0] < cut:
            q.popleft()

    def window(self, mint: str, seconds: float) -> list[tuple]:
        cut = time.time() - seconds
        return [x for x in self.d.get(mint, ()) if x[0] >= cut]

    def change_pct(self, mint: str, seconds: float) -> float | None:
        w = self.window(mint, seconds)
        if len(w) < 2 or w[0][1] <= 0:
            return None
        if w[-1][0] - w[0][0] < seconds * 0.6:
            return None
        return 100 * (w[-1][1] / w[0][1] - 1)

    def rise_from_low_pct(self, mint: str, seconds: float) -> float | None:
        w = self.window(mint, seconds)
        if len(w) < 2:
            return None
        low = min(x[1] for x in w if x[1] > 0) if any(x[1] > 0 for x in w) else 0
        return 100 * (w[-1][1] / low - 1) if low else None

    def high(self, mint: str, seconds: float) -> float | None:
        w = self.window(mint, seconds)
        return max(x[1] for x in w) if w else None

    def liq_max(self, mint: str, seconds: float) -> float | None:
        w = self.window(mint, seconds)
        return max(x[2] for x in w) if w else None

    def liq_trend_ok(self, mint: str) -> bool | None:
        w = self.window(mint, 1800)
        if len(w) < 3:
            return None
        return w[-1][2] >= 0.95 * max(x[2] for x in w)
