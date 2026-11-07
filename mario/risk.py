"""Risk engine (directive §6, §8). Independent of the AI. Never loosens itself.

User session limits (max trade size, max loss %, duration) are applied *on top*
of the directive limits - whichever is stricter wins.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .journal import Journal

log = logging.getLogger("mario.risk")

STOP_GAP_BUFFER_PCT = 3.0     # stops on a DEX can fill worse than the trigger


@dataclass
class Session:
    session_id: str
    mode: str                    # paper | live
    max_trade_sol: float
    max_loss_pct: float
    started_at: float
    ends_at: float
    start_equity: float


@dataclass
class Gate:
    can_enter: bool
    reasons: list = field(default_factory=list)
    stop_everything: bool = False


def expected_reward_pct(s: Settings) -> float:
    """Average exit of the TP ladder if it plays out; remainder assumed to exit at the last TP."""
    levels = list(s.tp_levels)
    n = len(levels)
    rest = max(0.0, 1 - n * s.tp_fraction)
    return sum(l * s.tp_fraction for l in levels) + rest * (levels[-1] if levels else 0)


def reward_risk(s: Settings, stop_pct: float, cost_pct: float) -> float:
    return (expected_reward_pct(s) - cost_pct) / (stop_pct + cost_pct)


class RiskEngine:
    def __init__(self, s: Settings, j: Journal, mode: str):
        self.s, self.j, self.mode = s, j, mode
        self.key = f"risk_state_{mode}"
        self.session: Session | None = None

    # ---- persistent state
    def state(self) -> dict:
        return self.j.get(self.key, {}) or {}

    def save(self, st: dict):
        self.j.put(self.key, st)

    def roll(self, equity: float) -> dict:
        st = self.state()
        today = dt.date.today()
        day, week = today.isoformat(), f"{today.isocalendar()[0]}-W{today.isocalendar()[1]}"
        if st.get("day") != day:
            st["day"], st["day_start_equity"] = day, equity
        if st.get("week") != week:
            st["week"], st["week_start_equity"] = week, equity
        st["peak_equity"] = max(st.get("peak_equity", equity), equity)
        st.setdefault("consecutive_losses", 0)
        self.save(st)
        return st

    # ---- gate for new entries
    def check(self, equity: float, exposure_sol: float, open_positions: int) -> Gate:
        s, st = self.s, self.roll(equity)
        g = Gate(True)

        def block(r, everything=False):
            g.can_enter = False
            g.reasons.append(r)
            g.stop_everything = g.stop_everything or everything

        if st.get("disabled"):
            block(f"strategy disabled: {st.get('disabled_reason')} - run `python -m mario reactivate` after review")
        day_pnl = 100 * (equity / st["day_start_equity"] - 1) if st.get("day_start_equity") else 0
        week_pnl = 100 * (equity / st["week_start_equity"] - 1) if st.get("week_start_equity") else 0
        dd = 100 * (1 - equity / st["peak_equity"]) if st.get("peak_equity") else 0
        if day_pnl <= -s.daily_loss_pct:
            block(f"daily loss limit hit ({day_pnl:.2f}%)")
        if week_pnl <= -s.weekly_loss_pct:
            block(f"weekly loss limit hit ({week_pnl:.2f}%)")
        if dd >= s.max_drawdown_pct:
            st["disabled"], st["disabled_reason"] = True, f"max drawdown {dd:.2f}%"
            self.save(st)
            block(f"max drawdown reached ({dd:.2f}%) - manual review required")
        if st.get("pause_until", 0) > time.time():
            block(f"paused after {s.consecutive_loss_pause} consecutive losses until "
                  f"{dt.datetime.fromtimestamp(st['pause_until']):%H:%M}")
        if open_positions >= s.max_positions:
            block(f"{open_positions} positions open (max {s.max_positions})")
        if exposure_sol >= equity * s.max_exposure_pct / 100:
            block("portfolio exposure limit reached")
        if self.session:
            ss = self.session
            sess_pnl = 100 * (equity / ss.start_equity - 1) if ss.start_equity else 0
            if sess_pnl <= -ss.max_loss_pct:
                block(f"session max loss {ss.max_loss_pct}% reached ({sess_pnl:.2f}%)", everything=True)
            if time.time() >= ss.ends_at:
                block("session duration ended", everything=True)
        return g

    # ---- sizing
    def size(self, equity: float, exposure_sol: float, stop_pct: float, cost_pct: float) -> tuple[float, str]:
        s = self.s
        eff_stop = stop_pct + cost_pct + STOP_GAP_BUFFER_PCT
        by_risk = equity * (s.risk_per_trade_pct / 100) / (eff_stop / 100)
        cap = equity * s.max_position_pct / 100
        room = equity * s.max_exposure_pct / 100 - exposure_sol
        options = {"risk": by_risk, "position cap": cap, "exposure room": room}
        if self.session:
            options["your max trade"] = self.session.max_trade_sol
        limiter = min(options, key=options.get)
        size = max(0.0, options[limiter])
        if size < s.network_fee_sol * 20:          # fees would dominate the trade
            return 0.0, f"size {size:.4f} SOL too small vs fees (limited by {limiter})"
        return size, limiter

    # ---- results
    def record_close(self, pnl_sol: float):
        st = self.state()
        if pnl_sol < 0:
            st["consecutive_losses"] = st.get("consecutive_losses", 0) + 1
            n = st["consecutive_losses"]
            if n >= self.s.consecutive_loss_disable:
                st["disabled"], st["disabled_reason"] = True, f"{n} consecutive losses"
                log.warning("strategy DISABLED after %s consecutive losses", n)
            elif n >= self.s.consecutive_loss_pause:
                st["pause_until"] = time.time() + 60 * self.s.pause_after_losses_min
                log.warning("pausing entries for %s min after %s consecutive losses", self.s.pause_after_losses_min, n)
        else:
            st["consecutive_losses"] = 0
        self.save(st)

    def reactivate(self):
        st = self.state()
        st.pop("disabled", None)
        st.pop("disabled_reason", None)
        st["pause_until"] = 0
        st["consecutive_losses"] = 0
        st.pop("peak_equity", None)      # new baseline after manual review
        self.save(st)
