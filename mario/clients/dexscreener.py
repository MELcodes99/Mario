"""DexScreener public API (pairs: ~300 req/min, profiles/boosts: ~60 req/min)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..util import Http, RateLimiter


@dataclass
class Pair:
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    quote_mint: str
    quote_symbol: str
    price_usd: float
    price_native: float
    liquidity_usd: float
    liq_base: float
    liq_quote: float
    fdv: float
    market_cap: float
    created_at_ms: int
    txns: dict
    volume: dict
    change: dict
    websites: list = field(default_factory=list)
    socials: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)

    @property
    def age_min(self) -> float:
        if not self.created_at_ms:
            return -1
        return (time.time() * 1000 - self.created_at_ms) / 60000

    @property
    def mc(self) -> float:
        return self.market_cap or self.fdv

    def buys(self, w: str) -> int:
        return int((self.txns.get(w) or {}).get("buys") or 0)

    def sells(self, w: str) -> int:
        return int((self.txns.get(w) or {}).get("sells") or 0)

    def vol(self, w: str) -> float:
        return float(self.volume.get(w) or 0)

    def chg(self, w: str) -> float:
        return float(self.change.get(w) or 0)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_pair(p: dict) -> Pair | None:
    if p.get("chainId") != "solana":
        return None
    base, quote = p.get("baseToken") or {}, p.get("quoteToken") or {}
    liq = p.get("liquidity") or {}
    info = p.get("info") or {}
    return Pair(
        mint=base.get("address", ""), symbol=base.get("symbol", "?"), name=base.get("name", ""),
        pair_address=p.get("pairAddress", ""), dex_id=p.get("dexId", ""),
        quote_mint=quote.get("address", ""), quote_symbol=quote.get("symbol", ""),
        price_usd=_f(p.get("priceUsd")), price_native=_f(p.get("priceNative")),
        liquidity_usd=_f(liq.get("usd")), liq_base=_f(liq.get("base")), liq_quote=_f(liq.get("quote")),
        fdv=_f(p.get("fdv")), market_cap=_f(p.get("marketCap")),
        created_at_ms=int(p.get("pairCreatedAt") or 0),
        txns=p.get("txns") or {}, volume=p.get("volume") or {}, change=p.get("priceChange") or {},
        websites=[w.get("url") for w in info.get("websites") or [] if isinstance(w, dict)],
        socials=[{"type": s.get("type"), "url": s.get("url")} for s in info.get("socials") or [] if isinstance(s, dict)],
        labels=p.get("labels") or [],
    )


class DexScreener:
    def __init__(self, http: Http, base: str):
        self.http, self.base = http, base.rstrip("/")
        self.pairs_limiter = RateLimiter(4.0)
        self.meta_limiter = RateLimiter(0.9)

    async def latest_profiles(self) -> list[dict]:
        await self.meta_limiter.wait()
        r = await self.http.get(f"{self.base}/token-profiles/latest/v1") or []
        return [x for x in r if x.get("chainId") == "solana"]

    async def latest_boosts(self) -> list[dict]:
        await self.meta_limiter.wait()
        r = await self.http.get(f"{self.base}/token-boosts/latest/v1") or []
        return [x for x in r if x.get("chainId") == "solana"]

    async def tokens(self, mints: list[str]) -> dict[str, Pair]:
        """Best (deepest) pair per mint where the mint is the base token."""
        out: dict[str, Pair] = {}
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            await self.pairs_limiter.wait()
            r = await self.http.get(f"{self.base}/tokens/v1/solana/{','.join(chunk)}") or []
            for raw in r:
                p = parse_pair(raw)
                if not p or p.mint not in chunk:
                    continue
                if p.mint not in out or p.liquidity_usd > out[p.mint].liquidity_usd:
                    out[p.mint] = p
        return out
