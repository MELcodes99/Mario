"""Mario's autonomous loop (directive §10): discovery -> security gate -> scoring ->
strategy nomination -> risk -> simulation -> execution -> monitoring -> journal.

Modes
  scan  : never trades. "Calls" tokens that clear every gate, tracks their market cap,
          reports every hour and at end of day (ranked highest to lowest).
  trade : paper (default) or live. User sets max trade size, max session loss %, and a
          1/4/8/12/24h or custom cycle. Directive limits still apply - stricter wins.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from . import reports
from .clients.anthropic_intel import AnthropicIntel
from .clients.dexscreener import DexScreener, Pair
from .clients.jupiter import SOL_MINT, Jupiter
from .clients.pumpportal import PumpPortalStream
from .clients.rpc import LAMPORTS, SolanaRPC
from .clients.rugcheck import RugCheck
from .clients.telegram import Telegram
from .config import Settings
from .execution import ExecutionRejected, LiveExecutor, PaperExecutor
from .journal import Journal
from .market import History, MarketWatch
from .positions import PositionManager
from .risk import RiskEngine, Session, reward_risk
from .scoring import score_token
from .security import BLOCKED_EXTENSIONS, GateInputs, evaluate
from .smart_wallets import SmartWallets
from .strategies import nominate
from .util import Http, TTLCache, fnum

log = logging.getLogger("mario.engine")

SCAN_NOMINAL_SOL = 0.5
HELP_TEXT = ("🍄 Mario commands:\n/status - what Mario is doing\n/report - report for the last hour now\n"
             "/pause - no new calls/entries (open positions stay protected)\n/resume - continue\n"
             "/stop - close positions, write report, then wait for /resume")   # size used for sellability/impact checks in scan mode


@dataclass
class WatchItem:
    mint: str
    first_seen: float
    tags: set = field(default_factory=set)
    migrated_at: float | None = None
    pair: Pair | None = None
    pinned: bool = False
    wait_retest_since: float | None = None
    next_eval: float = 0.0


def read_control(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def write_control(path: str, **kw):
    c = read_control(path)
    c.update(kw)
    c["updated"] = time.time()
    Path(path).write_text(json.dumps(c))


class Mario:
    def __init__(self, s: Settings, mode: str, live: bool = False, keypair=None,
                 max_trade_sol: float = 0.1, max_loss_pct: float = 3.0, hours: float = 4.0):
        self.s, self.mode, self.live = s, mode, live
        self.http = Http()
        self.j = Journal(s.db_path)
        self.rpc = SolanaRPC(self.http, s.rpc_url)
        self.dex = DexScreener(self.http, s.dexscreener_base)
        self.rug = RugCheck(self.http, s.rugcheck_base, s.rugcheck_api_key)
        self.jup = Jupiter(self.http, s.jupiter_base, s.jupiter_api_key, s.jupiter_rps)
        self.intel = AnthropicIntel(self.http, s.anthropic_api_key, s.anthropic_model,
                                    s.anthropic_web_search, s.anthropic_web_search_tool)
        self.tg = Telegram(self.http, s.telegram_bot_token, s.telegram_chat_id,
                           str(Path(s.data_dir) / "telegram_offset"))
        self.hist = History()
        self.market = MarketWatch(self.dex)
        self.wallets = SmartWallets(self.j, self.rpc)
        self.stream = PumpPortalStream(s.pumpportal_ws, self.on_stream_event)
        self.watch: dict[str, WatchItem] = {}
        self.rejected = TTLCache(24 * 3600)
        self.sec_cache = TTLCache(s.security_cache_ttl)
        self.ai_cache = TTLCache(s.ai_cache_ttl)
        self.tz = ZoneInfo(s.report_tz)
        self.stats = {"launches": 0, "migrations": 0, "evaluated": 0, "reject_reasons": {}}
        self.paused = False
        self.stopping = False
        self.done = asyncio.Event()
        self.shutdown_reason = ""
        self.keypair = keypair
        exec_mode = "live" if live else "paper"
        if mode == "trade":
            self.ex = LiveExecutor(s, self.jup, self.rpc, keypair, self.j) if live else PaperExecutor(s, self.jup, self.j)
            self.risk = RiskEngine(s, self.j, exec_mode)
            self.pm = PositionManager(s, self.j, self.ex, self.risk, notify=self.notify)
            self.session_params = (max_trade_sol, max_loss_pct, hours)
        else:
            self.ex = self.risk = self.pm = None
        self.session: Session | None = None

    # ------------------------------------------------------------------ helpers
    def day(self) -> str:
        return dt.datetime.now(self.tz).date().isoformat()

    async def notify(self, text: str):
        await self.tg.send(text)

    def reject_reason(self, key: str):
        k = key.split(":")[0]
        self.stats["reject_reasons"][k] = self.stats["reject_reasons"].get(k, 0) + 1

    def add_watch(self, mint: str, tag: str, migrated_at: float | None = None):
        if self.rejected.get(mint) or self.j.blacklisted(mint):
            return
        it = self.watch.get(mint)
        if not it:
            if len(self.watch) >= self.s.watch_max_tokens:
                self.evict()
            it = self.watch[mint] = WatchItem(mint, time.time())
        it.tags.add(tag)
        if migrated_at:
            it.migrated_at = migrated_at

    def evict(self):
        victims = sorted((w for w in self.watch.values() if not w.pinned),
                         key=lambda w: (w.pair.liquidity_usd if w.pair else 0))
        for w in victims[:max(1, len(victims) // 10)]:
            self.watch.pop(w.mint, None)

    async def equity(self) -> float:
        cash = await self.ex.cash_sol()
        return cash + self.pm.exposure_sol()

    # ------------------------------------------------------------------ discovery
    async def on_stream_event(self, kind: str, ev: dict):
        mint = ev.get("mint")
        if not mint:
            return
        if kind == "new_token":
            self.stats["launches"] += 1          # tracked, not traded: too young by rule
        elif kind == "migration":
            self.stats["migrations"] += 1
            self.add_watch(mint, "migrated", time.time())
        elif kind == "trade":
            trader = ev.get("traderPublicKey")
            if trader in set(self.wallets.active()):
                w = self.watch.get(mint)
                price = w.pair.price_usd if w and w.pair else None
                self.wallets.record(trader, mint, ev["txType"], float(ev.get("solAmount") or 0), price)
                if ev["txType"] == "buy":
                    self.add_watch(mint, "smart_wallet")

    async def discovery_loop(self):
        while not self.stopping:
            if not self.paused:
                try:
                    for x in await self.dex.latest_profiles():
                        self.add_watch(x["tokenAddress"], "profile")
                    for x in await self.dex.latest_boosts():
                        self.add_watch(x["tokenAddress"], "boosted_paid")
                except Exception as e:
                    log.warning("dexscreener discovery failed: %s", e)
                for m in self.wallets.recent_buy_mints():
                    self.add_watch(m, "smart_wallet")
            await asyncio.sleep(self.s.discovery_interval)

    async def wallet_loop(self):
        await self.stream.watch_accounts(self.wallets.active())
        while not self.stopping:
            try:
                price_of = lambda m: (self.watch[m].pair.price_usd if m in self.watch and self.watch[m].pair else None)
                for w, mint, side in await self.wallets.poll_once(price_of):
                    log.info("smart wallet %s… %s %s", w[:6], side, mint)
                    if side == "buy":
                        self.add_watch(mint, "smart_wallet")
                self.wallets.grade(self.peak_after, lambda m: price_of(m))
            except Exception as e:
                log.warning("wallet poll failed: %s", e)
            await asyncio.sleep(self.s.wallet_poll_interval)

    def peak_after(self, mint, t0, t1):
        pts = [x[1] for x in self.hist.d.get(mint, ()) if t0 <= x[0] <= t1]
        return max(pts) if pts else None

    # ------------------------------------------------------------------ evaluation
    async def refresh_pairs(self) -> dict[str, Pair]:
        mints = list(self.watch)
        if self.pm:
            mints += [p.mint for p in self.pm.open.values() if p.mint not in self.watch]
        pairs = await self.dex.tokens(mints) if mints else {}
        for m, p in pairs.items():
            self.hist.add(p)
            if m in self.watch:
                self.watch[m].pair = p
        cutoff = time.time() - self.s.max_pair_age_h * 3600
        for m, w in list(self.watch.items()):
            if not w.pinned and w.first_seen < cutoff:
                self.watch.pop(m, None)
        return pairs

    async def security(self, it: WatchItem, size_sol: float):
        p = it.pair
        cached = self.sec_cache.get(p.mint)
        if cached:
            return cached
        mi = rug = None
        try:
            mi = await self.rpc.get_mint_info(p.mint)
        except Exception as e:
            log.debug("mint info %s: %s", p.mint, e)
        try:
            rug = await self.rug.report(p.mint)
        except Exception as e:
            log.debug("rugcheck %s: %s", p.mint, e)
        creator = rug.creator if rug else None
        g = GateInputs(pair=p, mint_info=mi, rug=rug, sol_price_usd=self.market.regime.sol_price,
                       liq_max_15m=self.hist.liq_max(p.mint, 900),
                       deployer_blacklisted=bool(creator and self.j.blacklisted(creator)),
                       quote_error="quotes not requested yet")
        r = evaluate(g, self.s)
        static_fail = [k for k, v in r.checks.items() if v[0] != "pass" and k not in ("sell_route", "price_impact")]
        if not static_fail:
            try:
                g.buy_quote = await self.jup.order(SOL_MINT, p.mint, int(size_sol * LAMPORTS))
                g.sell_quote = await self.jup.order(p.mint, SOL_MINT, int(g.buy_quote.out_amount))
                g.probe_quote = await self.jup.order(SOL_MINT, p.mint, int(0.01 * LAMPORTS))
                g.quote_error = None
            except Exception as e:
                g.quote_error = f"no executable route: {e}"
            r = evaluate(g, self.s)
        out = (r, g, rug)
        self.sec_cache.set(p.mint, out)
        return out

    async def ai_for(self, p: Pair, it: WatchItem):
        if not self.intel.enabled:
            return None
        c = self.ai_cache.get(p.mint)
        if c is not None:
            return c or None
        data = {"mint": p.mint, "name": p.name, "symbol": p.symbol, "dex": p.dex_id, "age_min": round(p.age_min),
                "market_cap_usd": p.mc, "liquidity_usd": p.liquidity_usd, "price_change": p.change,
                "volume": p.volume, "txns": p.txns, "websites": p.websites, "socials": p.socials,
                "discovery_tags": sorted(it.tags)}
        intel, raw = await self.intel.analyze(p.mint, data)
        self.j.ai(p.mint, intel is not None, raw)
        self.ai_cache.set(p.mint, intel or False)
        return intel

    async def evaluate_token(self, it: WatchItem):
        p, s = it.pair, self.s
        if not p or time.time() < it.next_eval:
            return
        if self.j.blacklisted(p.mint) or (self.pm and self.pm.holds(p.mint)):
            return
        if p.age_min < s.min_pair_age_min:
            return self.reject_reason("pair younger than 15m")
        if p.age_min > s.max_pair_age_h * 60 and not it.pinned:
            return self.reject_reason("pair older than 48h")
        if p.liquidity_usd < s.min_liquidity_usd:
            return self.reject_reason("liquidity below $50k")
        self.stats["evaluated"] += 1
        it.next_eval = time.time() + s.evaluate_interval * 2

        rg = self.market.regime
        size = SCAN_NOMINAL_SOL
        if self.mode == "trade":
            eq = await self.equity()
            size, _ = self.risk.size(eq, self.pm.exposure_sol(), s.stop_loss_pct, 2.0)
            if size <= 0:
                size = 0.05
        sec, g, rug = await self.security(it, size)
        if not sec.passed:
            reason = sec.failures[0]
            self.reject_reason(reason)
            self.j.decision(self.mode, p, sec, None, [], "reject", "; ".join(sec.failures)[:900])
            if sec.permanent:
                self.rejected.set(p.mint, True)
                self.watch.pop(p.mint, None)
            else:
                it.next_eval = time.time() + s.security_cache_ttl
            return
        lp_locked = max([m.lp_locked_pct or 0 for m in rug.markets] or [0]) if rug else None
        impact = g.buy_quote.price_impact_pct if g.buy_quote else None
        confl = self.wallets.confluence(p.mint)
        pre = score_token(p, self.hist, rg, None, len(confl), impact, lp_locked)
        intel = None
        if pre.total - pre.parts["narrative"] + 20 >= s.min_score:     # AI only when it could matter
            intel = await self.ai_for(p, it)
        sc = score_token(p, self.hist, rg, intel, len(confl), impact, lp_locked) if intel else pre
        noms = nominate(p, it.tags, it.migrated_at, self.hist, intel, confl, time.time())
        names = [n.strategy for n in noms]
        bar = s.scan_call_score if self.mode == "scan" else s.min_score
        if not noms:
            self.reject_reason("no strategy setup")
            return self.j.decision(self.mode, p, sec, sc, names, "watch", "passed security; no strategy setup")
        if sc.total < bar:
            self.reject_reason(f"score below {bar:.0f}")
            return self.j.decision(self.mode, p, sec, sc, names, "watch", f"score {sc.total} < {bar}")
        if rg.extreme_risk_off or not rg.fresh:
            self.reject_reason("market regime")
            return self.j.decision(self.mode, p, sec, sc, names, "watch", f"market {rg.label}")
        if sc.fomo:
            if not it.wait_retest_since:
                it.wait_retest_since = time.time()
            if time.time() - it.wait_retest_since > s.retest_wait_max_min * 60:
                it.wait_retest_since = None
                self.rejected.set(p.mint, True)
            self.reject_reason("anti-FOMO wait")
            return self.j.decision(self.mode, p, sec, sc, names, "wait", "anti-FOMO: waiting for retest")
        it.wait_retest_since = None
        if self.mode == "scan":
            return self.make_call(p, sc, noms[0].strategy)
        await self.try_enter(it, sec, g, rug, sc, noms)

    # ------------------------------------------------------------------ scan calls
    def make_call(self, p: Pair, sc, strategy: str):
        day = self.day()
        if self.j.q("SELECT 1 FROM calls WHERE day=? AND mint=?", (day, p.mint)):
            return
        self.j.x("""INSERT INTO calls(day,ts,mint,symbol,name,strategy,score,call_price,call_mc,call_liq,
                    last_price,last_mc,peak_price,peak_mc,peak_ts,low_price,last_update,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (day, time.time(), p.mint, p.symbol, p.name, strategy, sc.total, p.price_usd, p.mc,
                  p.liquidity_usd, p.price_usd, p.mc, p.price_usd, p.mc, time.time(), p.price_usd, time.time(), "open"))
        self.j.decision("scan", p, None, sc, [strategy], "call", f"score {sc.total}")
        self.watch[p.mint].pinned = True
        msg = (f"📣 MARIO CALL {p.symbol} ({p.mint})\nMC ${fnum(p.mc)} · liq ${fnum(p.liquidity_usd)} · "
               f"score {sc.total:.0f} · {strategy}\nhttps://dexscreener.com/solana/{p.pair_address}")
        log.info(msg.replace("\n", " | "))
        asyncio.create_task(self.notify(msg))

    def update_calls(self, pairs: dict[str, Pair]):
        for r in self.j.q("SELECT * FROM calls WHERE day=?", (self.day(),)):
            p = pairs.get(r["mint"])
            if r["mint"] in self.watch:
                self.watch[r["mint"]].pinned = True
            else:
                self.watch[r["mint"]] = WatchItem(r["mint"], time.time(), {"called"}, pinned=True)
            if not p or p.price_usd <= 0:
                continue
            peak = p.price_usd > (r["peak_price"] or 0)
            self.j.x("""UPDATE calls SET last_price=?, last_mc=?, peak_price=?, peak_mc=?, peak_ts=?, low_price=?,
                        last_update=? WHERE id=?""",
                     (p.price_usd, p.mc, p.price_usd if peak else r["peak_price"], p.mc if peak else r["peak_mc"],
                      time.time() if peak else r["peak_ts"], min(r["low_price"] or p.price_usd, p.price_usd),
                      time.time(), r["id"]))

    # ------------------------------------------------------------------ trading
    async def try_enter(self, it, sec, g, rug, sc, noms):
        p, s = it.pair, self.s
        eq = await self.equity()
        gate = self.risk.check(eq, self.pm.exposure_sol(), len(self.pm.open))
        if not gate.can_enter:
            self.reject_reason("risk gate")
            return self.j.decision("trade", p, sec, sc, [n.strategy for n in noms], "skip", "; ".join(gate.reasons))
        size, limiter = self.risk.size(eq, self.pm.exposure_sol(), s.stop_loss_pct, 2.0)
        if size <= 0:
            return self.j.decision("trade", p, sec, sc, [], "skip", limiter)
        buy_q = g.buy_quote
        fee_pct = 100 * 2 * s.network_fee_sol / size + 2 * (buy_q.fee_bps / 100 if buy_q else 0)
        cost = (buy_q.price_impact_pct if buy_q else 2) + (g.sell_quote.price_impact_pct if g.sell_quote else 2) + fee_pct
        rr = reward_risk(s, s.stop_loss_pct, cost)
        if rr < s.min_reward_risk:
            self.reject_reason("reward/risk < 2")
            return self.j.decision("trade", p, sec, sc, [], "skip", f"net reward/risk {rr:.2f} < {s.min_reward_risk}")
        slip = s.max_slippage_pct_liquid if p.liquidity_usd >= 100_000 else s.max_slippage_pct_volatile
        if self.live and s.live_test_trade:
            ok = await self.test_trade(p)
            if not ok:
                return
        try:
            fill = await self.ex.buy(p.mint, size, slip)
        except (ExecutionRejected, ValueError) as e:
            self.reject_reason("execution rejected")
            return self.j.decision("trade", p, sec, sc, [], "skip", f"execution rejected: {e}")
        reason = " + ".join(n.reason for n in noms)
        pos = self.pm.open_position(fill, p, noms[0].strategy, sc.total, rug.creator if rug else None,
                                    self.session.session_id, reason, slip)
        self.j.decision("trade", p, sec, sc, [n.strategy for n in noms], "buy", reason)
        msg = (f"🟦 BUY {p.symbol} [{self.ex.mode}] {fill.sol:.4f} SOL (limited by {limiter}) · score {sc.total:.0f} · "
               f"{noms[0].strategy} · R/R {rr:.1f} · MC ${fnum(p.mc)}\nstop -{s.stop_loss_pct:.0f}% · TPs "
               + "/".join(f"+{x:.0f}%" for x in s.tp_levels) + f" · trail {s.trailing_pct:.0f}%")
        log.info(msg.replace("\n", " | "))
        await self.notify(msg)
        return pos

    async def test_trade(self, p: Pair) -> bool:
        try:
            f = await self.ex.buy(p.mint, self.s.live_test_trade_sol, self.s.max_slippage_pct_volatile)
            await self.ex.sell(p.mint, f.tokens, self.s.max_slippage_pct_volatile)
            return True
        except Exception as e:
            log.warning("[%s] live test trade failed (%s) - blacklisting", p.symbol, e)
            self.j.blacklist(p.mint, "mint", f"test trade failed: {e}")
            return False

    async def emergency_check(self, pos) -> str | None:
        try:
            mi = await self.rpc.get_mint_info(pos.mint)
        except Exception:
            return None
        if mi.mint_authority or mi.freeze_authority:
            return "a mint/freeze authority appeared"
        bad = [e.get("extension") for e in mi.extensions if e.get("extension") in BLOCKED_EXTENSIONS]
        if bad:
            return f"dangerous token extension now active: {bad}"
        return None

    async def position_loop(self):
        while True:
            try:
                if self.pm.open:
                    pairs = await self.dex.tokens([p.mint for p in self.pm.open.values()])
                    await self.pm.monitor(pairs, self.emergency_check)
            except Exception as e:
                log.warning("position monitor error: %s", e)
            if self.stopping and not self.pm.open:
                return
            await asyncio.sleep(self.s.position_interval)

    # ------------------------------------------------------------------ main loops
    async def evaluate_loop(self):
        while not self.stopping:
            try:
                ups = [w.pair for w in self.watch.values() if w.pair]
                breadth = (sum(p.chg("h1") > 0 for p in ups) / len(ups)) if len(ups) >= 10 else None
                await self.market.refresh(breadth)
                pairs = await self.refresh_pairs()
                if self.mode == "scan":
                    self.update_calls(pairs)
                if not self.paused:
                    for it in sorted(self.watch.values(), key=lambda w: -(w.pair.liquidity_usd if w.pair else 0)):
                        if self.stopping:
                            break
                        if self.mode == "trade" and self.session and time.time() >= self.session.ends_at:
                            break
                        await self.evaluate_token(it)
            except Exception as e:
                log.warning("evaluation pass failed: %s", e)
                log.debug("traceback", exc_info=True)
            await asyncio.sleep(self.s.evaluate_interval)

    async def control_loop(self):
        while not self.stopping:
            c = read_control(self.s.control_path)
            for cmd in await self.tg.commands():
                if cmd in ("pause", "resume", "stop"):
                    write_control(self.s.control_path, paused=(cmd == "pause"), stop=(cmd == "stop"))
                    c = read_control(self.s.control_path)
                elif cmd == "status":
                    await self.notify(await self.status_text())
                elif cmd == "report":
                    await self.publish_hourly(time.time() - 3600, reset=False)
                elif cmd in ("help", "start"):
                    await self.notify(HELP_TEXT)
            if bool(c.get("paused")) != self.paused:
                self.paused = bool(c.get("paused"))
                msg = "⏸ Mario PAUSED (no new calls/entries; open positions still protected)" if self.paused \
                    else "▶️ Mario RESUMED"
                log.info(msg)
                await self.notify(msg)
            if c.get("stop"):
                log.info("stop requested")
                write_control(self.s.control_path, stop=False)
                await self.shutdown("stop requested")
                return
            if self.mode == "trade" and self.session and time.time() >= self.session.ends_at:
                await self.shutdown("session duration ended")
                return
            if self.mode == "trade":
                eq = await self.equity()
                g = self.risk.check(eq, self.pm.exposure_sol(), 0)
                if g.stop_everything:
                    await self.shutdown("; ".join(g.reasons))
                    return
            await asyncio.sleep(5)

    async def status_text(self) -> str:
        t = [f"Mario [{self.mode}{'/live' if self.live else ''}] {'PAUSED' if self.paused else 'running'} · "
             f"watching {len(self.watch)} · market {self.market.regime.label}"]
        if self.mode == "trade":
            eq = await self.equity()
            t.append(f"equity {eq:.4f} SOL · open {len(self.pm.open)} · exposure {self.pm.exposure_sol():.4f} SOL")
            if self.session:
                t.append(f"session ends {dt.datetime.fromtimestamp(self.session.ends_at, self.tz):%H:%M}")
            for p in self.pm.open.values():
                v = p.last_value_sol
                t.append(f"  {p.symbol}: in {p.sol_in:.4f}, value {v if v is None else round(v, 4)}, "
                         f"TPs {len(p.tp_hits)}, stop x{p.stop_ratio:.2f}")
        return "\n".join(t)

    async def report_loop(self):
        last_hour = time.time()
        last_day = self.day()
        while not self.stopping:
            await asyncio.sleep(20)
            now = time.time()
            if self.day() != last_day:
                await self.publish_eod(last_day)
                last_day = self.day()
            if now - last_hour >= 3600:
                await self.publish_hourly(last_hour)
                last_hour = now

    async def publish_hourly(self, since: float, reset: bool = True):
        if self.mode == "scan":
            text = reports.hourly_report(self.j, self.day(), self.s.report_tz, since, self.stats["evaluated"],
                                         self.stats["reject_reasons"])
            text += f"\nPump.fun launches seen: {self.stats['launches']} · graduations: {self.stats['migrations']}"
        else:
            text = "🍄 MARIO hourly trade status\n" + await self.status_text()
        reports.save_report(self.s.reports_dir, f"hourly-{dt.datetime.now(self.tz):%H%M}", text)
        print("\n" + text + "\n")
        await self.notify(text)
        if reset:
            self.stats.update(launches=0, migrations=0, evaluated=0, reject_reasons={})

    async def publish_eod(self, day: str):
        text = reports.eod_report(self.j, day, self.s.report_tz)
        rows = reports.call_rows(self.j, day)
        p = reports.save_report(self.s.reports_dir, f"eod-{day}", text, {"day": day, "calls": rows})
        print("\n" + text + "\n")
        log.info("end-of-day report saved to %s", p)
        await self.notify(text)

    async def shutdown(self, reason: str):
        if self.stopping:
            return
        self.stopping = True
        self.shutdown_reason = reason
        self.stream.stop()
        log.info("shutting down: %s", reason)
        if self.mode == "trade":
            await self.pm.close_all(f"session end: {reason}")
            for _ in range(30):
                if not self.pm.open:
                    break
                await asyncio.sleep(10)
                await self.pm.close_all(f"session end retry: {reason}")
            end_eq = await self.equity()
            text = reports.session_report(self.j, self.session.session_id, self.ex.mode,
                                          self.session.start_equity, end_eq)
            if self.pm.open:
                text += "\n\n⚠️ Some positions could not be sold; check them manually: " + \
                        ", ".join(f"{p.symbol} {p.mint}" for p in self.pm.open.values())
            reports.save_report(self.s.reports_dir, f"session-{self.session.session_id}", text)
            print("\n" + text + "\n")
            await self.notify(text)
        else:
            text = reports.hourly_report(self.j, self.day(), self.s.report_tz, time.time() - 3600,
                                         self.stats["evaluated"], self.stats["reject_reasons"])
            reports.save_report(self.s.reports_dir, "final-scan-snapshot", text)
            print("\n" + text + "\n")
        self.done.set()

    async def run(self):
        write_control(self.s.control_path, stop=False)
        self.paused = bool(read_control(self.s.control_path).get("paused"))
        tasks = [self.stream.run(), self.discovery_loop(), self.wallet_loop(), self.evaluate_loop(),
                 self.report_loop(), self.control_loop()]
        if self.mode == "trade":
            max_trade, max_loss, hours = self.session_params
            eq = await self.equity()
            sid = f"{'live' if self.live else 'paper'}-{dt.datetime.now(self.tz):%Y%m%d-%H%M%S}"
            self.session = Session(sid, self.ex.mode, max_trade, max_loss, time.time(), time.time() + hours * 3600, eq)
            self.risk.session = self.session
            tasks.append(self.position_loop())
            msg = (f"🍄 Mario trade session {sid} started · equity {eq:.4f} SOL · max trade {max_trade} SOL · "
                   f"max loss {max_loss}% · {hours}h · directive caps: {self.s.risk_per_trade_pct}% risk/trade, "
                   f"{self.s.max_position_pct}% per position, {self.s.max_positions} positions")
        else:
            msg = "🍄 Mario scan mode started - calls only, no trading. Hourly + end-of-day reports."
        log.info(msg)
        await self.notify(msg)
        if not self.intel.enabled:
            log.warning("ANTHROPIC_API_KEY not set: narrative scoring is heuristic only (max 5/20), "
                        "so very few tokens can reach 85")
        if not self.stream.available:
            log.warning("install `websockets` for live Pump.fun launches/graduations/wallet trades")
        try:
            await asyncio.gather(*[asyncio.create_task(t) for t in tasks])
        except asyncio.CancelledError:
            pass
        finally:
            await self.http.aclose()

    async def run_until_stopped(self):
        import signal
        loop = asyncio.get_running_loop()
        hits = {"n": 0}

        def on_sigint():
            hits["n"] += 1
            if hits["n"] == 1:
                log.info("Ctrl+C: graceful shutdown (press again to force)")
                asyncio.create_task(self.shutdown("interrupted by user"))
            else:
                raise KeyboardInterrupt
        try:
            loop.add_signal_handler(signal.SIGINT, on_sigint)
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(
                self.shutdown("host is stopping/redeploying the service (SIGTERM)")))
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C raises KeyboardInterrupt directly
        main = asyncio.create_task(self.run())
        done = asyncio.create_task(self.done.wait())
        await asyncio.wait({main, done}, return_when=asyncio.FIRST_COMPLETED)
        main.cancel()
        try:
            await main
        except asyncio.CancelledError:
            pass
