"""Entry strategies Mario runs. Each one only *nominates* a token; every nominee
still has to clear the security gate, score >= 85, risk engine and simulation.

Research basis (see STRATEGIES.md):
  1. smart_money_confluence - copy trading is the dominant memecoin entry strategy,
     but single-wallet copying is farmed by manipulative bots -> require >=2
     independently graded wallets and never chase.
  2. graduation_retest      - <1-2% of Pump.fun launches graduate; the first minutes
     after migration are the most volatile, so wait >=15m and buy a held retest.
  3. volume_breakout_retest - classic breakout-with-volume, entered on the retest.
  4. narrative_early        - AI finds an emerging, organic narrative before saturation.

Deliberately NOT implemented: launch-second sniping (directive: experimental, disabled
until validated), bundled launches, volume bots / wash trading, KOL dump schemes
(those are market manipulation, not strategies).
"""
from __future__ import annotations

from dataclasses import dataclass

from .clients.anthropic_intel import Intel
from .clients.dexscreener import Pair
from .market import History


@dataclass
class Nomination:
    strategy: str
    reason: str


def smart_money_confluence(confluence: list[str]) -> Nomination | None:
    if len(confluence) >= 2:
        return Nomination("smart_money", f"{len(confluence)} graded smart wallets bought within 30m")
    return None


def graduation_retest(p: Pair, tags: set, migrated_at: float | None, h: History, now: float) -> Nomination | None:
    if "migrated" not in tags or not migrated_at:
        return None
    mins = (now - migrated_at) / 60
    if mins < 15:
        return None
    hi = h.high(p.mint, 3600)
    liq_ok = h.liq_trend_ok(p.mint)
    if hi and p.price_usd >= 0.7 * hi and p.price_usd <= 0.97 * hi and liq_ok is not False \
            and p.buys("h1") > p.sells("h1") and -8 <= p.chg("m5") <= 6:
        return Nomination("graduation_retest", f"graduated {mins:.0f}m ago, holding a retest of the post-migration high")
    return None


def volume_breakout_retest(p: Pair, h: History) -> Nomination | None:
    h6_avg = p.vol("h6") / 6 if p.vol("h6") else 0
    if not h6_avg:
        return None
    acc = p.vol("h1") / h6_avg
    hi = h.high(p.mint, 2 * 3600)
    if 1.5 <= acc <= 6 and p.chg("h1") > 8 and -7 <= p.chg("m5") <= 4 and hi and p.price_usd >= 0.88 * hi:
        return Nomination("volume_breakout", f"h1 volume {acc:.1f}x 6h average, consolidating near the high")
    return None


def narrative_early(intel: Intel | None) -> Nomination | None:
    if intel and intel.recommendation == "candidate" and intel.trend_stage in ("emerging", "accelerating") \
            and intel.organic_attention in ("high", "medium") and intel.confidence >= 60:
        return Nomination("narrative_early", f"AI: {intel.trend_stage} narrative - {intel.narrative[:80]}")
    return None


def nominate(p: Pair, tags: set, migrated_at, h: History, intel, confluence, now) -> list[Nomination]:
    out = []
    for n in (smart_money_confluence(confluence), graduation_retest(p, tags, migrated_at, h, now),
              volume_breakout_retest(p, h), narrative_early(intel)):
        if n:
            out.append(n)
    return out
