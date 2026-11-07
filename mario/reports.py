"""Hourly and end-of-day reports for scan mode, plus trade session reports.

"Peak x" is the best multiple reached after Mario's call - it is NOT a return
anyone actually captured. "Now x" is where the call stands at report time.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .journal import Journal
from .util import fnum


def _tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def fmt_ts(ts: float, tz) -> str:
    return dt.datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _x(a, b) -> float | None:
    return (a / b) if a and b else None


def call_rows(j: Journal, day: str) -> list[dict]:
    rows = []
    for r in j.q("SELECT * FROM calls WHERE day=?", (day,)):
        d = dict(r)
        d["peak_x"] = _x(r["peak_price"], r["call_price"]) or 1.0
        d["now_x"] = _x(r["last_price"], r["call_price"]) or 0.0
        rows.append(d)
    rows.sort(key=lambda d: d["peak_x"], reverse=True)
    return rows


def _table(rows: list[dict], tz) -> list[str]:
    out = ["| # | Called | Token | Strategy | Score | MC at call | Peak MC | Peak x | Now MC | Now x |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {fmt_ts(r['ts'], tz)} | {r['symbol']} `{r['mint'][:6]}…` | {r['strategy']} | "
                   f"{r['score']:.0f} | ${fnum(r['call_mc'])} | ${fnum(r['peak_mc'])} | {r['peak_x']:.2f}x | "
                   f"${fnum(r['last_mc'])} | {r['now_x']:.2f}x |")
    return out


def summary_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"calls": 0}
    return {
        "calls": n,
        "hit_1_5x": sum(r["peak_x"] >= 1.5 for r in rows),
        "hit_2x": sum(r["peak_x"] >= 2 for r in rows),
        "up_now": sum(r["now_x"] > 1 for r in rows),
        "down_50_now": sum(r["now_x"] <= 0.5 for r in rows),
        "median_peak_x": sorted(r["peak_x"] for r in rows)[n // 2],
        "median_now_x": sorted(r["now_x"] for r in rows)[n // 2],
        "best": rows[0]["symbol"], "best_peak_x": rows[0]["peak_x"],
    }


def hourly_report(j: Journal, day: str, tz_name: str, since_ts: float, scanned: int, rejected: dict) -> str:
    tz = _tz(tz_name)
    rows = call_rows(j, day)
    new = [r for r in rows if r["ts"] >= since_ts]
    st = summary_stats(rows)
    lines = [f"🍄 MARIO hourly scan report — {dt.datetime.now(tz):%Y-%m-%d %H:%M} ({tz_name})", "",
             f"Tokens evaluated this hour: {scanned}. New calls this hour: {len(new)}. Calls today: {st['calls']}."]
    if rejected:
        top = sorted(rejected.items(), key=lambda kv: -kv[1])[:6]
        lines.append("Top rejection reasons: " + ", ".join(f"{k} ({v})" for k, v in top))
    if new:
        lines += ["", "New calls this hour:"] + _table(sorted(new, key=lambda r: r["ts"]), tz)
    if rows:
        lines += ["", "All calls today, ranked by peak return since call:"] + _table(rows, tz)
        lines += ["", f"Reached 1.5x: {st['hit_1_5x']}/{st['calls']} · 2x: {st['hit_2x']}/{st['calls']} · "
                      f"above call price now: {st['up_now']}/{st['calls']} · down 50%+ now: {st['down_50_now']}"]
    else:
        lines += ["", "No token passed every security check and the score bar this hour. No call is a valid call."]
    lines += ["", "_Peak x = best multiple after the call (not a captured return). Fees/slippage not included._"]
    return "\n".join(lines)


def eod_report(j: Journal, day: str, tz_name: str) -> str:
    tz = _tz(tz_name)
    rows = call_rows(j, day)
    st = summary_stats(rows)
    dec = j.q("SELECT decision, COUNT(*) n FROM decisions WHERE ts>=? GROUP BY decision",
              (dt.datetime.fromisoformat(day).replace(tzinfo=tz).timestamp(),))
    lines = [f"🍄 MARIO end-of-day report — {day} ({tz_name})", "",
             f"Calls: {st['calls']}"]
    if dec:
        lines.append("Decisions logged: " + ", ".join(f"{r['decision']} {r['n']}" for r in dec))
    if rows:
        lines += [f"Best call: {st['best']} at {st['best_peak_x']:.2f}x peak",
                  f"Median peak: {st['median_peak_x']:.2f}x · median now: {st['median_now_x']:.2f}x",
                  f"Reached 1.5x: {st['hit_1_5x']} · 2x: {st['hit_2x']} · up now: {st['up_now']} · "
                  f"down 50%+ now: {st['down_50_now']}", "",
                  "Every call, ranked highest to lowest peak return:"] + _table(rows, tz)
    else:
        lines.append("No calls today.")
    lines += ["", "_Peak x = best multiple reached after the call (not a captured return)._"]
    return "\n".join(lines)


def session_report(j: Journal, session_id: str, mode: str, start_equity: float, end_equity: float) -> str:
    rows = j.q("SELECT * FROM positions WHERE session_id=? ORDER BY opened_ts", (session_id,))
    closed = [r for r in rows if r["status"] == "closed"]
    pnls = [r["realized_sol"] - r["sol_in"] - r["fees_sol"] for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    exp = (sum(pnls) / n) if n else 0
    lines = [f"🍄 MARIO trade session report [{mode}] {session_id}", "",
             f"Equity: {start_equity:.4f} → {end_equity:.4f} SOL ({100 * (end_equity / start_equity - 1):+.2f}%)"
             if start_equity else "",
             f"Closed trades: {n} · wins {len(wins)} · losses {len(losses)} · "
             f"win rate {100 * len(wins) / n:.0f}%" if n else "Closed trades: 0 (no valid setups - that is allowed)",
             f"Avg win {sum(wins) / len(wins):+.4f} SOL · avg loss {sum(losses) / len(losses):+.4f} SOL · "
             f"expectancy {exp:+.4f} SOL/trade" if wins and losses else "", ""]
    if rows:
        lines += ["| Token | Strategy | In SOL | Out SOL | PnL SOL | PnL % | Peak | Exit |", "|---|---|---|---|---|---|---|---|"]
        for r in rows:
            pnl = r["realized_sol"] - r["sol_in"] - r["fees_sol"]
            lines.append(f"| {r['symbol']} | {r['strategy']} | {r['sol_in']:.4f} | {r['realized_sol']:.4f} | "
                         f"{pnl:+.4f} | {100 * pnl / r['sol_in']:+.1f}% | {100 * ((r['peak_ratio'] or 1) - 1):+.0f}% | "
                         f"{r['exit_reason'] or r['status']} |")
    return "\n".join(l for l in lines if l is not None)


def save_report(reports_dir: Path, name: str, text: str, data: dict | None = None) -> Path:
    day_dir = reports_dir / dt.date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    p = day_dir / f"{name}.md"
    p.write_text(text)
    if data is not None:
        (day_dir / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))
    return p


def paper_track_record(j: Journal) -> dict:
    rows = j.q("SELECT realized_sol - sol_in - fees_sol AS pnl FROM positions WHERE mode='paper' AND status='closed'")
    pnls = [r["pnl"] for r in rows]
    n = len(pnls)
    return {"trades": n, "win_rate": (sum(p > 0 for p in pnls) / n) if n else 0,
            "net_sol": sum(pnls), "expectancy": (sum(pnls) / n) if n else 0, "ts": time.time()}
