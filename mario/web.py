"""Tiny web dashboard + health check (stdlib only), for hosting Mario as a web service.

GET  /health                 -> "ok" (Render health check / uptime pinger; no key needed)
GET  /?key=KEY               -> status, today's calls ranked, open positions, reports
GET  /reports/<day>/<file>?key=KEY
POST /control/<pause|resume|stop>?key=KEY

Runs in a background thread so it stays up between trade sessions and while halted.
If DASHBOARD_KEY is empty the dashboard is disabled (health check still works).
"""
from __future__ import annotations

import datetime as dt
import hmac
import html
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import reports
from .config import Settings
from .engine import read_control, write_control
from .journal import Journal
from .util import fnum

log = logging.getLogger("mario.web")

CSS = """body{font:14px -apple-system,system-ui,sans-serif;margin:24px;max-width:1100px;color:#1b1b1b;background:#fafaf7}
h1{font-size:22px}h2{font-size:16px;margin-top:28px}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #e4e4dc;padding:6px 8px;text-align:left}th{background:#f0f0ea}
.pill{display:inline-block;padding:2px 10px;border-radius:10px;background:#e7f3e7}.halt{background:#fbe3e1}
button{padding:6px 14px;margin-right:6px;border:1px solid #bbb;border-radius:6px;background:#fff;cursor:pointer}
.muted{color:#777}"""


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def render_dashboard(s: Settings, key: str) -> str:
    j = Journal(s.db_path)
    c = read_control(s.control_path)
    day = dt.datetime.now().date().isoformat()
    try:
        from zoneinfo import ZoneInfo
        day = dt.datetime.now(ZoneInfo(s.report_tz)).date().isoformat()
    except Exception:
        pass
    state = "HALTED: " + _esc(c.get("halted_reason")) if c.get("halted") else ("PAUSED" if c.get("paused") else "running")
    out = [f"<html><head><meta name=viewport content='width=device-width'><title>Mario</title><style>{CSS}</style>"
           f"<meta http-equiv=refresh content=60></head><body><h1>🍄 Mario</h1>",
           f"<p><span class='pill {'halt' if c.get('halted') or c.get('paused') else ''}'>{state}</span> "
           f"<span class=muted>{dt.datetime.now():%Y-%m-%d %H:%M:%S} · auto-refresh 60s</span></p>",
           "<form method=post style='display:inline'>"]
    for a in ("pause", "resume", "stop"):
        out.append(f"<button formaction='/control/{a}?key={_esc(key)}'>{a}</button>")
    out.append("</form>")
    for mode in ("paper", "live"):
        st = j.get(f"risk_state_{mode}")
        if st:
            out.append(f"<p class=muted>{mode}: consecutive losses {st.get('consecutive_losses', 0)}"
                       + (f" · <b>DISABLED</b> ({_esc(st.get('disabled_reason'))})" if st.get("disabled") else "") + "</p>")
    rec = reports.paper_track_record(j)
    out.append(f"<p class=muted>paper record: {rec['trades']} closed trades · win rate {100 * rec['win_rate']:.0f}% · "
               f"net {rec['net_sol']:+.4f} SOL · paper cash {j.get('paper_cash')}</p>")

    pos = j.q("SELECT * FROM positions WHERE status='open'")
    out.append(f"<h2>Open positions ({len(pos)})</h2>")
    if pos:
        out.append("<table><tr><th>Mode</th><th>Token</th><th>Strategy</th><th>In SOL</th><th>Realized SOL</th>"
                   "<th>Peak</th><th>Stop</th><th>Opened</th></tr>")
        for r in pos:
            out.append(f"<tr><td>{r['mode']}</td><td>{_esc(r['symbol'])}</td><td>{_esc(r['strategy'])}</td>"
                       f"<td>{r['sol_in']:.4f}</td><td>{r['realized_sol']:.4f}</td>"
                       f"<td>{100 * ((r['peak_ratio'] or 1) - 1):+.0f}%</td><td>x{r['stop_ratio']:.2f}</td>"
                       f"<td>{dt.datetime.fromtimestamp(r['opened_ts']):%H:%M}</td></tr>")
        out.append("</table>")

    rows = reports.call_rows(j, day)
    out.append(f"<h2>Calls today ({day}), ranked by peak return</h2>")
    if rows:
        out.append("<table><tr><th>#</th><th>Called</th><th>Token</th><th>Strategy</th><th>Score</th>"
                   "<th>MC at call</th><th>Peak MC</th><th>Peak x</th><th>Now MC</th><th>Now x</th></tr>")
        for i, r in enumerate(rows, 1):
            out.append(f"<tr><td>{i}</td><td>{dt.datetime.fromtimestamp(r['ts']):%H:%M}</td>"
                       f"<td><a href='https://dexscreener.com/solana/{_esc(r['mint'])}' target=_blank>{_esc(r['symbol'])}</a></td>"
                       f"<td>{_esc(r['strategy'])}</td><td>{r['score']:.0f}</td><td>${fnum(r['call_mc'])}</td>"
                       f"<td>${fnum(r['peak_mc'])}</td><td><b>{r['peak_x']:.2f}x</b></td><td>${fnum(r['last_mc'])}</td>"
                       f"<td>{r['now_x']:.2f}x</td></tr>")
        out.append("</table><p class=muted>Peak x = best multiple after the call, not a captured return.</p>")
    else:
        out.append("<p class=muted>No calls yet today. No call is a valid call.</p>")

    out.append("<h2>Reports</h2><ul>")
    days = sorted([p for p in s.reports_dir.iterdir() if p.is_dir()], reverse=True)[:7]
    for d in days:
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        links = " · ".join(f"<a href='/reports/{d.name}/{f.name}?key={_esc(key)}'>{_esc(f.stem)}</a>" for f in files[:30])
        out.append(f"<li><b>{d.name}</b>: {links or '<span class=muted>none</span>'}</li>")
    out.append("</ul>")
    dec = j.q("SELECT ts, symbol, decision, reason FROM decisions ORDER BY id DESC LIMIT 25")
    out.append("<h2>Latest decisions</h2><table><tr><th>Time</th><th>Token</th><th>Decision</th><th>Reason</th></tr>")
    for r in dec:
        out.append(f"<tr><td>{dt.datetime.fromtimestamp(r['ts']):%H:%M:%S}</td><td>{_esc(r['symbol'])}</td>"
                   f"<td>{_esc(r['decision'])}</td><td class=muted>{_esc((r['reason'] or '')[:160])}</td></tr>")
    out.append("</table></body></html>")
    return "".join(out)


def make_handler(s: Settings, key: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep Render logs clean
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _authed(self, q) -> bool:
            given = (q.get("key") or [""])[0]
            return bool(key) and hmac.compare_digest(given, key)

        def do_HEAD(self):
            self._send(200, "")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/health":
                return self._send(200, "ok", "text/plain")
            if not key:
                return self._send(200, "Mario is running. Set DASHBOARD_KEY to enable the dashboard.", "text/plain")
            if not self._authed(q):
                return self._send(401, "add ?key=YOUR_DASHBOARD_KEY", "text/plain")
            try:
                if u.path == "/":
                    return self._send(200, render_dashboard(s, key))
                if u.path.startswith("/reports/"):
                    parts = u.path.split("/")
                    if len(parts) == 4 and all(p and ".." not in p for p in parts[2:]):
                        f = s.reports_dir / parts[2] / parts[3]
                        if f.is_file() and f.resolve().is_relative_to(s.reports_dir.resolve()):
                            return self._send(200, f.read_text(), "text/plain; charset=utf-8")
                return self._send(404, "not found", "text/plain")
            except Exception as e:
                log.warning("dashboard error: %s", e)
                return self._send(500, "dashboard error", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                return self._send(401, "unauthorized", "text/plain")
            action = u.path.rsplit("/", 1)[-1]
            if u.path.startswith("/control/") and action in ("pause", "resume", "stop"):
                if action == "pause":
                    write_control(s.control_path, paused=True)
                elif action == "resume":
                    write_control(s.control_path, paused=False, halted=False)
                else:
                    write_control(s.control_path, stop=True)
                self.send_response(303)
                self.send_header("Location", f"/?key={key}")
                self.end_headers()
                return
            return self._send(404, "not found", "text/plain")
    return H


def start_web(s: Settings, port: int, key: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(s, key))
    threading.Thread(target=srv.serve_forever, daemon=True, name="mario-web").start()
    log.info("web dashboard listening on :%s (%s)", port, "dashboard enabled" if key else "health check only")
    return srv
