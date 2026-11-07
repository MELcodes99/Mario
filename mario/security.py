"""Token security gate (directive §3). Deterministic and non-negotiable.

Every check returns pass / fail / unknown. Unknown counts as fail.
Nothing in here can be overridden by a score, a strategy, or the AI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .clients.dexscreener import Pair
from .clients.jupiter import Quote
from .clients.rpc import MintInfo
from .clients.rugcheck import BURN_ADDRESSES, RugReport
from .config import Settings
from .solana_tx import TOKEN_2022_PROGRAM, TOKEN_PROGRAM

BLOCKED_EXTENSIONS = {
    "permanentDelegate": "permanent delegate can move anyone's tokens",
    "nonTransferable": "token cannot be transferred",
    "defaultAccountState": "new accounts can start frozen",
    "pausableConfig": "transfers can be paused",
    "confidentialTransferMint": "confidential transfers hide balances/flows",
    "confidentialTransferFeeConfig": "confidential transfer fees",
}
BENIGN_EXTENSIONS = {"metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
                     "tokenGroup", "tokenGroupMember"}
WARN_EXTENSIONS = {"mintCloseAuthority", "interestBearingConfig", "scaledUiAmountConfig"}
POOL_TYPES = ("amm", "pool", "lock", "burn", "program", "vault")
STABLE_SYMBOLS = {"USDC", "USDT", "USD1", "PYUSD"}


@dataclass
class SecurityResult:
    mint: str
    checks: dict = field(default_factory=dict)      # name -> (status, detail)
    warnings: list = field(default_factory=list)
    permanent: bool = False                          # failure that will never heal

    def ok(self, name, detail=""):
        self.checks[name] = ("pass", detail)

    def fail(self, name, detail, permanent=False):
        self.checks[name] = ("fail", detail)
        self.permanent = self.permanent or permanent

    def unknown(self, name, detail):
        self.checks[name] = ("unknown", detail)

    @property
    def failures(self) -> list[str]:
        return [f"{k}: {v[1]}" for k, v in self.checks.items() if v[0] != "pass"]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(v[0] == "pass" for v in self.checks.values())

    def as_dict(self):
        return {"passed": self.passed, "checks": self.checks, "warnings": self.warnings,
                "permanent": self.permanent}


@dataclass
class GateInputs:
    pair: Pair
    mint_info: MintInfo | None
    rug: RugReport | None
    sol_price_usd: float
    liq_max_15m: float | None = None
    buy_quote: Quote | None = None
    sell_quote: Quote | None = None
    probe_quote: Quote | None = None       # small buy, to measure impact independently
    quote_error: str | None = None
    deployer_blacklisted: bool = False


def check_authorities(r: SecurityResult, mi: MintInfo | None, rug: RugReport | None, s: Settings):
    if mi is None:
        r.unknown("mint_authority", "on-chain mint data unavailable")
        r.unknown("freeze_authority", "on-chain mint data unavailable")
        r.unknown("token_program", "on-chain mint data unavailable")
        return
    r.ok("mint_authority") if mi.mint_authority is None else r.fail("mint_authority", f"active: {mi.mint_authority}", True)
    r.ok("freeze_authority") if mi.freeze_authority is None else r.fail("freeze_authority", f"active: {mi.freeze_authority}", True)
    if rug is not None and (rug.mint_authority or rug.freeze_authority) and not (mi.mint_authority or mi.freeze_authority):
        r.warnings.append("RugCheck reports an authority that on-chain data does not - re-check")
        r.unknown("authority_crosscheck", "sources disagree on authorities")
    if mi.program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        r.fail("token_program", f"suspicious program {mi.program}", True)
        return
    r.ok("token_program", "token-2022" if mi.program == TOKEN_2022_PROGRAM else "spl-token")
    if mi.program == TOKEN_2022_PROGRAM or mi.extensions:
        check_extensions(r, mi.extensions, s)
    else:
        r.ok("extensions", "none")


def check_extensions(r: SecurityResult, exts: list[dict], s: Settings):
    problems = []
    for e in exts:
        name = e.get("extension", "?")
        state = e.get("state") or {}
        if name in BLOCKED_EXTENSIONS:
            problems.append(f"{name} ({BLOCKED_EXTENSIONS[name]})")
        elif name == "transferHook":
            if state.get("programId"):
                problems.append(f"transferHook -> {state.get('programId')} (unexplained transfer hook)")
            elif state.get("authority"):
                problems.append("transferHook authority can install a hook later")
        elif name == "transferFeeConfig":
            bps = max(int((state.get("newerTransferFee") or {}).get("transferFeeBasisPoints", 0) or 0),
                      int((state.get("olderTransferFee") or {}).get("transferFeeBasisPoints", 0) or 0))
            if bps > s.max_transfer_fee_bps:
                problems.append(f"transfer fee {bps} bps")
            if state.get("transferFeeConfigAuthority"):
                problems.append("transfer fee can be raised by an authority")
        elif name in WARN_EXTENSIONS:
            r.warnings.append(f"extension {name}")
        elif name not in BENIGN_EXTENSIONS:
            problems.append(f"unrecognised extension {name}")
    if problems:
        r.fail("extensions", "; ".join(problems), True)
    else:
        r.ok("extensions", ",".join(e.get("extension", "?") for e in exts) or "none")


def _pool_accounts(rug: RugReport, pair: Pair) -> set[str]:
    accs = {pair.pair_address} | set(BURN_ADDRESSES)
    for m in rug.markets:
        accs |= m.accounts
        accs.add(m.pubkey)
    for addr, t in rug.known_accounts.items():
        if any(x in (t or "").lower() for x in POOL_TYPES):
            accs.add(addr)
    return accs


def check_holders(r: SecurityResult, rug: RugReport | None, pair: Pair, s: Settings):
    if rug is None or not rug.top_holders:
        r.unknown("holders", "holder distribution cannot be verified")
        return
    pool = _pool_accounts(rug, pair)
    holders = [h for h in rug.top_holders if h.owner not in pool and h.address not in pool]
    holders.sort(key=lambda h: h.pct, reverse=True)
    top1 = holders[0].pct if holders else 0.0
    top10 = sum(h.pct for h in holders[:10])
    detail = f"top1 {top1:.1f}%, top10 {top10:.1f}%"
    if top1 > s.max_top_holder_pct:
        r.fail("holders", f"largest non-pool wallet {top1:.1f}% > {s.max_top_holder_pct}%")
    elif top10 > s.max_top10_pct:
        r.fail("holders", f"top10 non-pool wallets {top10:.1f}% > {s.max_top10_pct}%")
    else:
        r.ok("holders", detail)
    insiders = [h for h in holders if h.insider]
    if rug.graph_insiders_detected > 0 or rug.insider_networks or len(insiders) >= 2:
        r.fail("insiders", f"related/insider wallet clusters detected "
                           f"(graph={rug.graph_insiders_detected}, flagged={len(insiders)})")
    else:
        r.ok("insiders")
    dep = rug.creator_balance_pct
    if rug.creator:
        dep = max(dep or 0.0, sum(h.pct for h in holders if h.owner == rug.creator))
    if dep is None:
        r.unknown("deployer_holdings", "deployer unknown")
    elif dep > s.max_deployer_pct:
        r.fail("deployer_holdings", f"deployer holds {dep:.1f}%")
    else:
        r.ok("deployer_holdings", f"{dep:.1f}%")


def check_liquidity(r: SecurityResult, g: GateInputs, s: Settings):
    p, rug = g.pair, g.rug
    if p.liquidity_usd < s.min_liquidity_usd:
        r.fail("liquidity", f"${p.liquidity_usd:,.0f} < ${s.min_liquidity_usd:,.0f}")
    else:
        r.ok("liquidity", f"${p.liquidity_usd:,.0f}")
    if p.quote_symbol.upper() in STABLE_SYMBOLS:
        exit_usd = p.liq_quote
    elif p.quote_symbol.upper() in ("SOL", "WSOL") and g.sol_price_usd > 0:
        exit_usd = p.liq_quote * g.sol_price_usd
    else:
        exit_usd = p.liquidity_usd / 2
    if exit_usd < s.min_exit_liquidity_usd:
        r.fail("exit_liquidity", f"exit side ${exit_usd:,.0f} < ${s.min_exit_liquidity_usd:,.0f}")
    else:
        r.ok("exit_liquidity", f"${exit_usd:,.0f}")
    if rug is None:
        r.unknown("lp_ownership", "LP lock/burn status unavailable")
    else:
        mk = [m for m in rug.markets if m.pubkey == p.pair_address] or rug.markets
        locked = [m.lp_locked_pct for m in mk if m.lp_locked_pct is not None]
        if not locked:
            r.unknown("lp_ownership", "LP lock not verifiable on-chain")
        elif max(locked) < s.min_lp_locked_pct:
            r.fail("lp_ownership", f"only {max(locked):.0f}% of LP locked/burned")
        else:
            r.ok("lp_ownership", f"{max(locked):.0f}% locked/burned")
        if rug.total_market_liquidity and p.liquidity_usd:
            a, b = rug.total_market_liquidity, p.liquidity_usd
            if abs(a - b) / max(a, b) > s.liquidity_crosscheck_tolerance:
                r.fail("liquidity_confirmed", f"DexScreener ${b:,.0f} vs RugCheck ${a:,.0f}")
            else:
                r.ok("liquidity_confirmed")
    if g.liq_max_15m and g.liq_max_15m > 0:
        drop = 100 * (1 - p.liquidity_usd / g.liq_max_15m)
        if drop > s.max_liquidity_drop_pct_15m:
            r.fail("liquidity_stability", f"liquidity fell {drop:.0f}% in 15m")
        else:
            r.ok("liquidity_stability")


def check_sellability(r: SecurityResult, g: GateInputs, s: Settings):
    if g.quote_error or g.buy_quote is None or g.sell_quote is None:
        r.fail("sell_route", g.quote_error or "no executable buy and sell route")
        return
    b, sq = g.buy_quote, g.sell_quote
    impact = max(b.price_impact_pct, sq.price_impact_pct)
    if g.probe_quote and g.probe_quote.in_amount and b.in_amount and g.probe_quote.out_amount:
        rate_small = g.probe_quote.out_amount / g.probe_quote.in_amount
        rate_full = b.out_amount / b.in_amount
        measured = max(0.0, 100 * (1 - rate_full / rate_small))
        impact = max(impact, measured)
    if impact > s.max_price_impact_pct:
        r.fail("price_impact", f"{impact:.2f}% > {s.max_price_impact_pct}%")
    else:
        r.ok("price_impact", f"{impact:.2f}%")
    back = sq.out_amount / b.in_amount if b.in_amount else 0
    loss = 100 * (1 - back)
    if loss > s.max_roundtrip_loss_pct:
        r.fail("sell_route", f"round-trip loses {loss:.1f}% (tax/honeypot/thin pool)")
    else:
        r.ok("sell_route", f"round-trip cost {loss:.2f}%")


def check_manipulation(r: SecurityResult, p: Pair, rug: RugReport | None, s: Settings):
    issues = []
    if p.liquidity_usd > 0 and p.vol("h1") / p.liquidity_usd > s.max_vol_to_liq_h1:
        issues.append(f"h1 volume {p.vol('h1') / p.liquidity_usd:.1f}x liquidity")
    b, se = p.buys("h1"), p.sells("h1")
    if b >= 30 and se < max(5, b / s.max_buy_sell_ratio):
        issues.append(f"buys/sells {b}/{se} (selling may be restricted)")
    tx = b + se
    if tx > 500 and p.vol("h1") / tx < s.min_avg_trade_usd:
        issues.append(f"avg trade ${p.vol('h1') / tx:.1f} (tiny-trade activity farming)")
    if rug:
        for name in rug.warn_risks() + rug.danger_risks():
            if any(w in name.lower() for w in ("wash", "bundle", "sniper", "copycat", "impersonat")):
                issues.append(f"rugcheck: {name}")
    if issues:
        r.fail("manipulation", "; ".join(issues))
    else:
        r.ok("manipulation")


def evaluate(g: GateInputs, s: Settings) -> SecurityResult:
    r = SecurityResult(g.pair.mint)
    check_authorities(r, g.mint_info, g.rug, s)
    if g.rug is None:
        r.unknown("rugcheck", "report unavailable")
    else:
        if g.rug.rugged:
            r.fail("rugcheck", "token flagged as rugged", True)
        elif g.rug.danger_risks():
            r.fail("rugcheck", "danger: " + ", ".join(g.rug.danger_risks()))
        else:
            r.ok("rugcheck")
        r.warnings += [f"rugcheck warn: {w}" for w in g.rug.warn_risks()]
    if g.deployer_blacklisted:
        r.fail("deployer_history", "deployer linked to a previous rug / blacklist", True)
    else:
        r.ok("deployer_history", "not in local rug database")
    if not g.pair.name or not g.pair.symbol:
        r.fail("metadata", "token identity is ambiguous (missing name/symbol)")
    else:
        r.ok("metadata")
    check_liquidity(r, g, s)
    check_holders(r, g.rug, g.pair, s)
    check_manipulation(r, g.pair, g.rug, s)
    check_sellability(r, g, s)
    return r
