"""Smart-wallet intelligence (directive §14 "wallet relationship intelligence").

You seed wallets (e.g. from GMGN / Birdeye top traders / Dune). Mario then
*grades them itself* from what it observes: every buy is recorded, and 6h later
it is scored by how the token moved. Wallets that flip within seconds of buying
(the copy-trader-baiting pattern documented by Luo et al., WWW'26) are penalised.
Only graded wallets count toward a confluence signal, and never a single wallet.
"""
from __future__ import annotations

import logging
import time

from .clients.rpc import STABLES, SolanaRPC, token_deltas_for_owner
from .journal import Journal
from .util import is_pubkey

log = logging.getLogger("mario.wallets")

EVAL_AFTER_S = 6 * 3600
WIN_RETURN = 0.25        # +25% peak within 6h = the wallet's call worked
LOSS_RETURN = -0.30
QUICK_FLIP_S = 120


class SmartWallets:
    def __init__(self, j: Journal, rpc: SolanaRPC | None = None):
        self.j, self.rpc = j, rpc

    # ---- book
    def add(self, address: str, label: str = "", source: str = "manual") -> bool:
        if not is_pubkey(address):
            return False
        self.j.x("INSERT OR IGNORE INTO smart_wallets(address,label,source,added_ts) VALUES(?,?,?,?)",
                 (address, label, source, time.time()))
        return True

    def remove(self, address: str):
        self.j.x("UPDATE smart_wallets SET active=0 WHERE address=?", (address,))

    def active(self) -> list[str]:
        return [r["address"] for r in self.j.q("SELECT address FROM smart_wallets WHERE active=1")]

    def rows(self):
        return self.j.q("SELECT * FROM smart_wallets ORDER BY active DESC, (wins - losses - quick_flips) DESC")

    def quality(self, address: str) -> float:
        """0..1. Unproven wallets get 0.5 until they have >=5 graded calls."""
        r = self.j.q("SELECT * FROM smart_wallets WHERE address=?", (address,))
        if not r:
            return 0.0
        r = r[0]
        graded = r["wins"] + r["losses"]
        if graded < 5:
            return 0.5 if r["quick_flips"] < 2 else 0.2
        wr = r["wins"] / graded
        return max(0.0, wr - 0.15 * r["quick_flips"] / max(graded, 1))

    # ---- signals
    def record(self, wallet: str, mint: str, side: str, sol: float, price: float | None):
        if mint in STABLES:
            return
        now = time.time()
        self.j.x("INSERT INTO wallet_signals(ts,wallet,mint,side,sol,price) VALUES(?,?,?,?,?,?)",
                 (now, wallet, mint, side, sol, price))
        if side == "sell":
            buy = self.j.q("SELECT ts FROM wallet_signals WHERE wallet=? AND mint=? AND side='buy' "
                           "ORDER BY ts DESC LIMIT 1", (wallet, mint))
            if buy and now - buy[0]["ts"] < QUICK_FLIP_S:
                self.j.x("UPDATE smart_wallets SET quick_flips=quick_flips+1 WHERE address=?", (wallet,))
        else:
            self.j.x("UPDATE smart_wallets SET signals=signals+1 WHERE address=?", (wallet,))

    def confluence(self, mint: str, window_s: int = 1800, min_quality: float = 0.5) -> list[str]:
        rows = self.j.q("SELECT DISTINCT wallet FROM wallet_signals WHERE mint=? AND side='buy' AND ts>=?",
                        (mint, time.time() - window_s))
        return [r["wallet"] for r in rows if self.quality(r["wallet"]) >= min_quality]

    def recent_buy_mints(self, window_s: int = 1800) -> list[str]:
        return [r["mint"] for r in self.j.q(
            "SELECT DISTINCT mint FROM wallet_signals WHERE side='buy' AND ts>=?", (time.time() - window_s,))]

    def grade(self, peak_price_after, now_price):
        """peak_price_after(mint, since_ts, until_ts) -> float|None, supplied by the engine's history."""
        rows = self.j.q("SELECT * FROM wallet_signals WHERE side='buy' AND evaluated=0 AND ts<=?",
                        (time.time() - EVAL_AFTER_S,))
        for r in rows:
            if not r["price"]:
                self.j.x("UPDATE wallet_signals SET evaluated=1 WHERE id=?", (r["id"],))
                continue
            peak = peak_price_after(r["mint"], r["ts"], r["ts"] + EVAL_AFTER_S)
            cur = now_price(r["mint"])
            if peak is None and cur is None:
                continue
            ret_peak = (peak or 0) / r["price"] - 1
            ret_now = (cur or 0) / r["price"] - 1
            outcome = ret_peak
            if ret_peak >= WIN_RETURN:
                self.j.x("UPDATE smart_wallets SET wins=wins+1, sum_return=sum_return+? WHERE address=?",
                         (ret_peak, r["wallet"]))
            elif ret_now <= LOSS_RETURN or ret_peak < 0.05:
                self.j.x("UPDATE smart_wallets SET losses=losses+1, sum_return=sum_return+? WHERE address=?",
                         (ret_now, r["wallet"]))
            self.j.x("UPDATE wallet_signals SET evaluated=1, outcome=? WHERE id=?", (outcome, r["id"]))
        # auto-retire wallets that turned out to be bad or manipulative
        for w in self.j.q("SELECT * FROM smart_wallets WHERE active=1"):
            graded = w["wins"] + w["losses"]
            if (graded >= 10 and w["wins"] / graded < 0.3) or w["quick_flips"] >= 5:
                self.j.x("UPDATE smart_wallets SET active=0 WHERE address=?", (w["address"],))
                log.info("retired smart wallet %s (wins %s / graded %s, quick flips %s)",
                         w["address"], w["wins"], graded, w["quick_flips"])

    # ---- RPC poller (DEX-agnostic: reads token balance changes of each tx)
    async def poll_once(self, price_of) -> list[tuple[str, str, str]]:
        if self.rpc is None:
            return []
        seen = []
        for row in self.j.q("SELECT address,last_sig FROM smart_wallets WHERE active=1"):
            w, last = row["address"], row["last_sig"]
            try:
                sigs = await self.rpc.get_signatures(w, limit=10, until=last)
            except Exception as e:
                log.debug("signatures %s: %s", w, e)
                continue
            if not sigs:
                continue
            self.j.x("UPDATE smart_wallets SET last_sig=? WHERE address=?", (sigs[0]["signature"], w))
            if last is None:          # first sight: set cursor, don't replay history
                continue
            for s in reversed(sigs):
                if s.get("err"):
                    continue
                try:
                    tx = await self.rpc.get_transaction(s["signature"])
                except Exception:
                    continue
                if not tx:
                    continue
                sol_d, deltas = token_deltas_for_owner(tx, w)
                for mint, d in deltas.items():
                    if mint in STABLES:
                        continue
                    side = "buy" if d > 0 and sol_d < 0 else "sell" if d < 0 else None
                    if side:
                        self.record(w, mint, side, abs(sol_d), price_of(mint))
                        seen.append((w, mint, side))
        return seen
