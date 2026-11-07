"""RugCheck token report. A useful risk signal - never treated as proof of safety."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import Http, RateLimiter

BURN_ADDRESSES = {"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"}


@dataclass
class Holder:
    address: str          # token account
    owner: str            # wallet that owns it
    pct: float            # percent of supply (0-100)
    insider: bool = False


@dataclass
class Market:
    pubkey: str
    market_type: str
    lp_locked_pct: float | None
    accounts: set = field(default_factory=set)   # pool vaults/authorities to exclude from holders


@dataclass
class RugReport:
    mint: str
    mint_authority: str | None
    freeze_authority: str | None
    token_program: str
    creator: str | None
    creator_balance_pct: float | None
    risks: list[dict]
    top_holders: list[Holder]
    known_accounts: dict[str, str]
    markets: list[Market]
    rugged: bool
    transfer_fee_pct: float
    total_market_liquidity: float | None
    graph_insiders_detected: int
    insider_networks: list
    raw: dict

    def danger_risks(self) -> list[str]:
        return [r.get("name", "?") for r in self.risks if str(r.get("level", "")).lower() == "danger"]

    def warn_risks(self) -> list[str]:
        return [r.get("name", "?") for r in self.risks if str(r.get("level", "")).lower() == "warn"]


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_report(mint: str, r: dict) -> RugReport:
    token = r.get("token") or {}
    known = {}
    for addr, v in (r.get("knownAccounts") or {}).items():
        known[addr] = (v or {}).get("type", "") if isinstance(v, dict) else str(v)
    markets = []
    for m in r.get("markets") or []:
        lp = m.get("lp") or {}
        accs = set()
        for k in ("pubkey", "liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount", "mintLP",
                  "mintLPAccount"):
            v = m.get(k)
            if isinstance(v, str) and v:
                accs.add(v)
            elif isinstance(v, dict):          # RugCheck returns some of these as account objects
                accs |= {x for x in (v.get("pubkey"), v.get("owner"), v.get("mint")) if isinstance(x, str) and x}
        markets.append(Market(m.get("pubkey", ""), m.get("marketType", ""), _f(lp.get("lpLockedPct")), accs))
    holders = [Holder(h.get("address", ""), h.get("owner", ""), _f(h.get("pct"), 0.0), bool(h.get("insider")))
               for h in r.get("topHolders") or []]
    tf = r.get("transferFee") or {}
    creator_bal = None
    supply = _f(token.get("supply"))
    if r.get("creatorBalance") is not None and supply:
        creator_bal = 100.0 * _f(r.get("creatorBalance"), 0.0) / supply
    return RugReport(
        mint=mint,
        mint_authority=r.get("mintAuthority", token.get("mintAuthority")),
        freeze_authority=r.get("freezeAuthority", token.get("freezeAuthority")),
        token_program=r.get("tokenProgram", ""),
        creator=r.get("creator"),
        creator_balance_pct=creator_bal,
        risks=r.get("risks") or [],
        top_holders=holders,
        known_accounts=known,
        markets=markets,
        rugged=bool(r.get("rugged")),
        transfer_fee_pct=_f(tf.get("pct"), 0.0) or 0.0,
        total_market_liquidity=_f(r.get("totalMarketLiquidity")),
        graph_insiders_detected=int(r.get("graphInsidersDetected") or 0),
        insider_networks=r.get("insiderNetworks") or [],
        raw=r,
    )


class RugCheck:
    def __init__(self, http: Http, base: str, api_key: str = ""):
        self.http, self.base = http, base.rstrip("/")
        self.headers = {"X-API-KEY": api_key} if api_key else None
        self.limiter = RateLimiter(1.0)

    async def report(self, mint: str) -> RugReport:
        await self.limiter.wait()
        r = await self.http.get(f"{self.base}/tokens/{mint}/report", headers=self.headers)
        if not isinstance(r, dict):
            raise ValueError("empty rugcheck report")
        return parse_report(mint, r)
