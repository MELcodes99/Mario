"""Mario Opportunity Score, 100 points (directive §4).

Narrative 20 | Organic momentum 25 | Liquidity quality 20 | Price structure 20 | Market 15
Only tokens that already passed the security gate are scored.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .clients.anthropic_intel import Intel
from .clients.dexscreener import Pair
from .market import History, Regime


@dataclass
class Score:
    total: float
    parts: dict
    notes: list = field(default_factory=list)
    fomo: bool = False                # anti-FOMO: >35% in 15m -> wait for retest


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def narrative_points(intel: Intel | None, pair: Pair, notes: list) -> float:
    if intel is None:
        # No AI analysis: only a weak, capped heuristic (socials present is NOT legitimacy)
        types = {(s.get("type") or "").lower() for s in pair.socials}
        pts = 2.0 * bool(pair.websites) + 2.0 * ("twitter" in types or "x" in types) + 1.0 * ("telegram" in types)
        notes.append("narrative: no AI analysis, heuristic only (max 5)")
        return pts
    if intel.recommendation == "reject" or intel.manipulation_signals and intel.organic_attention == "low":
        notes.append("narrative: AI flagged reject/manipulation")
        return 0.0
    pts = 0.0
    if intel.trend_stage in ("emerging", "accelerating"):
        pts += 8 * clamp(intel.attention_acceleration / 100, 0, 1)
    pts += {"high": 4, "medium": 2, "low": 0}[intel.source_quality]
    pts += 4 * clamp((intel.narrative_score - 40) / 50, 0, 1)
    pts += {"emerging": 4, "accelerating": 2}.get(intel.trend_stage, 0)
    if intel.organic_attention in ("low", "unknown"):
        pts *= 0.5
        notes.append("narrative: attention not shown to be organic")
    return pts


def momentum_points(p: Pair, wallet_confluence: int, notes: list) -> float:
    pts = 0.0
    b5, s5, b1, s1 = p.buys("m5"), p.sells("m5"), p.buys("h1"), p.sells("h1")
    r1 = b1 / max(s1, 1)
    r5 = b5 / max(s5, 1)
    # sustained organic buying pressure (but very lopsided = warning, not bullish)
    if 1.1 <= r1 <= 3.0 and 1.0 <= r5 <= 3.5:
        pts += 8
    elif 1.0 <= r1 <= 4.0:
        pts += 4
    # increasing independent buyers: last hour vs. 6h hourly average
    h6_avg_buys = p.buys("h6") / 6 if p.buys("h6") else 0
    if h6_avg_buys and b1 >= 1.3 * h6_avg_buys:
        pts += 6
    elif h6_avg_buys and b1 >= h6_avg_buys:
        pts += 3
    elif not h6_avg_buys and b1 >= 100:     # pair younger than 1h of h6 data
        pts += 3
    # healthy volume acceleration (1.2x-5x, not parabolic)
    h6_avg_vol = p.vol("h6") / 6 if p.vol("h6") else 0
    if h6_avg_vol:
        acc = p.vol("h1") / h6_avg_vol
        pts += 5 if 1.2 <= acc <= 5 else (2 if 1.0 <= acc < 1.2 else 0)
    # positive buy/sell imbalance
    pts += 3 if 1.05 <= r1 <= 2.5 else 0
    # buyer retention: sellers not overtaking buyers in the last 5m
    pts += 3 if s5 <= b5 * 1.2 and b5 > 0 else 0
    if wallet_confluence >= 2:
        notes.append(f"momentum: {wallet_confluence} tracked smart wallets bought")
        pts += min(5, 2 * wallet_confluence)
    return clamp(pts, 0, 25)


def liquidity_points(p: Pair, impact_pct: float | None, lp_locked: float | None, liq_stable: bool | None) -> float:
    pts = 0.0
    L = p.liquidity_usd
    pts += 10 if L >= 250_000 else 8 if L >= 100_000 else 5 if L >= 50_000 else 0
    if impact_pct is not None:
        pts += 5 if impact_pct <= 0.5 else 3 if impact_pct <= 1.0 else 1 if impact_pct <= 2.0 else 0
    pts += 3 if (lp_locked or 0) >= 99 else 0
    pts += 2 if liq_stable else 0
    return clamp(pts, 0, 20)


def structure_points(p: Pair, h: History, notes: list) -> tuple[float, bool]:
    pts = 0.0
    m5, h1, h6 = p.chg("m5"), p.chg("h1"), p.chg("h6")
    rise15 = h.rise_from_low_pct(p.mint, 900)
    fomo = (rise15 is not None and rise15 > 35) or (rise15 is None and (m5 > 20 or (h1 > 35 and m5 > 8)))
    if fomo:
        notes.append("structure: >35% in ~15m - anti-FOMO wait for consolidation/retest")
    # higher highs / higher lows proxy
    if h1 > 0 and h6 > 0:
        pts += 5
    elif h1 > 0:
        pts += 2
    # healthy consolidation after an up-move
    if h1 > 5 and -6 <= m5 <= 6:
        pts += 5
    # breakout supported by volume: near recent high with rising volume
    hi = h.high(p.mint, 3600)
    if hi and p.price_usd >= 0.95 * hi and p.vol("m5") * 12 >= p.vol("h1") * 0.8:
        pts += 5
    # low abnormal volatility
    if abs(m5) < 12 and abs(h1) < 80:
        pts += 5
    if h1 > 200 or h6 > 500:
        notes.append("structure: already extremely extended")
        pts = min(pts, 5)
    return clamp(pts, 0, 20), fomo


def market_points(rg: Regime, notes: list) -> float:
    if not rg.fresh:
        notes.append("market: data stale")
        return 0.0
    pts = 0.0
    pts += 5 if rg.sol_h1 > -1.5 else 2 if rg.sol_h1 > -3 else 0
    pts += 5 if rg.sol_h24 > -5 else 2 if rg.sol_h24 > -8 else 0
    if rg.btc_h24 is not None:
        pts += 3 if rg.btc_h24 > -4 else 1 if rg.btc_h24 > -7 else 0
    if rg.meme_breadth is not None:
        pts += 2 if rg.meme_breadth >= 0.4 else 0
    return clamp(pts, 0, 15)


def score_token(p: Pair, h: History, rg: Regime, intel: Intel | None, wallet_confluence: int,
                impact_pct: float | None, lp_locked: float | None) -> Score:
    notes: list = []
    parts = {"narrative": round(narrative_points(intel, p, notes), 1),
             "momentum": round(momentum_points(p, wallet_confluence, notes), 1),
             "liquidity": round(liquidity_points(p, impact_pct, lp_locked, h.liq_trend_ok(p.mint)), 1)}
    sp, fomo = structure_points(p, h, notes)
    parts["structure"] = round(sp, 1)
    parts["market"] = round(market_points(rg, notes), 1)
    return Score(round(sum(parts.values()), 1), parts, notes, fomo)
