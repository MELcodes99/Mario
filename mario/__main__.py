"""Mario CLI.   python -m mario            (interactive menu)
               python -m mario scan
               python -m mario trade --max-trade 0.2 --max-loss 3 --duration 4 [--live]
               python -m mario pause | resume | stop | status
               python -m mario wallet new | import | show | export-key | withdraw --to ADDR --amount 0.5|all
               python -m mario wallets add <address> [--label X] | list | remove <address>
               python -m mario report [--day YYYY-MM-DD]
               python -m mario reactivate [--live]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import reports
from .config import load_settings
from .engine import Mario, read_control, write_control
from .journal import Journal
from .risk import RiskEngine
from .smart_wallets import SmartWallets
from .util import setup_logging
from .wallet import Keypair, load_keypair, save_keypair, wallet_pubkey

DURATIONS = {"1": 1, "4": 4, "8": 8, "12": 12, "24": 24}

BANNER = r"""
  __  __    _    ____  ___ ___
 |  \/  |  / \  |  _ \|_ _/ _ \     Solana memecoin scanner / trader
 | |\/| | / _ \ | |_) || | | | |    capital preservation first.
 | |  | |/ ___ \|  _ < | | |_| |    no trade is a valid decision.
 |_|  |_/_/   \_\_| \_\___\___/
"""


def cmd_wallet(s, a):
    if a.action in ("new", "import"):
        if Path(s.wallet_path).exists():
            print(f"A wallet already exists at {s.wallet_path} ({wallet_pubkey(s.wallet_path)}).\n"
                  f"Mario keeps one trading wallet. Withdraw its funds, then move that file away to make a new one.")
            return
        if a.action == "new":
            kp = Keypair.generate_with_mnemonic(12)
        else:
            from .wallet import _wordlist, mnemonic_is_valid
            phrase = " ".join(getpass.getpass("Seed phrase (hidden while typing): ").lower().split())
            if not mnemonic_is_valid(phrase, _wordlist()):
                sys.exit("❌ that is not a valid 12/24-word seed phrase")
            kp = Keypair.from_mnemonic(phrase)
        pw = None
        if a.encrypt:
            pw = getpass.getpass("Choose a passphrase: ")
            if pw != getpass.getpass("Repeat passphrase: "):
                sys.exit("passphrases do not match")
        save_keypair(kp, s.wallet_path, pw)
        print(f"\n✅ Trading wallet {'created' if a.action == 'new' else 'imported'}\n"
              f"  address : {kp.pubkey}\n  file    : {s.wallet_path} (chmod 600{', encrypted' if pw else ''})")
        if a.action == "new":
            words = kp.mnemonic.split()
            print("\n  SEED PHRASE - write these 12 words on paper, in order. Anyone with them owns the wallet.\n")
            for i in range(0, 12, 4):
                print("   " + "   ".join(f"{n + 1:>2}. {w:<10}" for n, w in enumerate(words[i:i + 4], start=i)))
            print(f"\n  Restores in Phantom/Solflare: Add wallet → Import recovery phrase (path {kp.path}).")
        print("\nNext: fund it ONLY with what Mario may trade. Keep the rest in your own treasury wallet.")
    elif a.action == "show":
        pk = wallet_pubkey(s.wallet_path)
        if not pk:
            sys.exit("no wallet yet: python -m mario wallet new")
        from .clients.rpc import LAMPORTS, SolanaRPC
        from .util import Http

        async def bal():
            h = Http()
            try:
                return await SolanaRPC(h, s.rpc_url).get_balance(pk) / LAMPORTS
            finally:
                await h.aclose()
        try:
            b = asyncio.run(bal())
            print(f"{pk}\nbalance: {b:.6f} SOL")
        except Exception as e:
            print(f"{pk}\nbalance: unavailable ({e})")
    elif a.action == "withdraw":
        from .clients.rpc import SolanaRPC
        from .util import Http
        from .withdraw import WithdrawError, allowed_addresses, check_destination, withdraw_sol
        kp = load_keypair(s.wallet_path)
        to = a.to or (allowed_addresses()[0] if len(allowed_addresses()) == 1 else None)
        if not to:
            sys.exit("give --to ADDRESS (one of TREASURY_ADDRESS in .env)")
        try:
            check_destination(to, kp.pubkey)
        except WithdrawError as e:
            sys.exit(f"❌ {e}")
        amt = None if str(a.amount).lower() == "all" else float(a.amount)
        j = Journal(s.db_path)
        if j.q("SELECT 1 FROM positions WHERE status='open' AND mode='live'"):
            print("⚠️  Mario has open LIVE positions. Withdrawing may leave it unable to pay fees to exit them.")
        print(f"\nWithdraw {'ALL SOL (minus fee)' if amt is None else f'{amt} SOL'}\n  from {kp.pubkey}\n  to   {to}")
        if input("Type WITHDRAW to confirm: ").strip() != "WITHDRAW":
            sys.exit("cancelled")

        async def go():
            h = Http()
            try:
                return await withdraw_sol(SolanaRPC(h, s.rpc_url), kp, to, amt)
            finally:
                await h.aclose()
        try:
            sig, sent = asyncio.run(go())
        except WithdrawError as e:
            sys.exit(f"❌ {e}")
        print(f"✅ sent {sent:.6f} SOL\n   https://solscan.io/tx/{sig}")
    elif a.action == "export-key":
        print("⚠️  Anyone with this key controls the wallet. Never paste it into a website or chat.")
        if input("Type SHOW to display it: ").strip() != "SHOW":
            return
        kp = load_keypair(s.wallet_path)
        print(f"\naddress     : {kp.pubkey}\nprivate key : {kp.secret_b58()}\n(base58 - importable into Phantom/Solflare)")
        if kp.mnemonic:
            print(f"seed phrase : {kp.mnemonic}\n(path {kp.path})")


def cmd_wallets(s, a):
    sw = SmartWallets(Journal(s.db_path))
    if a.action == "add":
        for addr in a.address:
            print(("added " if sw.add(addr, a.label or "", "manual") else "invalid address ") + addr)
    elif a.action == "import":
        n = 0
        for line in Path(a.address[0]).read_text().splitlines():
            parts = line.split(",")
            if parts and sw.add(parts[0].strip(), parts[1].strip() if len(parts) > 1 else "", "import"):
                n += 1
        print(f"imported {n} wallets")
    elif a.action == "remove":
        for addr in a.address:
            sw.remove(addr)
            print("deactivated", addr)
    else:
        rows = sw.rows()
        if not rows:
            print("No smart wallets yet. Add some: python -m mario wallets add <address> --label name")
        for r in rows:
            g = r["wins"] + r["losses"]
            print(f"{'ON ' if r['active'] else 'off'} {r['address']}  {r['label'] or '':12} signals {r['signals']:4} "
                  f"graded {g:3} wins {r['wins']:3} quick-flips {r['quick_flips']:2} quality {sw.quality(r['address']):.2f}")


def live_gate(s) -> object:
    j = Journal(s.db_path)
    rec = reports.paper_track_record(j)
    print(f"\nPaper track record: {rec['trades']} closed trades, win rate {100 * rec['win_rate']:.0f}%, "
          f"net {rec['net_sol']:+.4f} SOL, expectancy {rec['expectancy']:+.5f} SOL/trade")
    if rec["trades"] < s.min_paper_trades_for_live:
        sys.exit(f"\nLive trading is locked until Mario has {s.min_paper_trades_for_live} closed paper trades "
                 f"(directive §12: validate before risking capital). Run paper sessions first, or change "
                 f"MIN_PAPER_TRADES_FOR_LIVE in .env after you have reviewed the results.")
    if rec["expectancy"] <= 0:
        print("⚠️  Paper expectancy is not positive. The directive says do not deploy a losing strategy.")
    kp = load_keypair(s.wallet_path)
    print(f"Trading wallet: {kp.pubkey}")
    if input("Type TRADE LIVE to risk real funds from this wallet: ").strip() != "TRADE LIVE":
        sys.exit("cancelled")
    return kp


def run_engine(m: Mario):
    write_control(m.s.control_path, paused=False)
    print("Running. Ctrl+C once = graceful stop (closes positions, writes report). "
          "Pause anytime from another terminal: python -m mario pause")
    try:
        asyncio.run(m.run_until_stopped())
    except KeyboardInterrupt:
        print("forced exit. Check open positions: python -m mario status")


def cmd_trade(s, a):
    if a.duration == "custom":
        if not a.hours or a.hours <= 0:
            sys.exit("--duration custom needs --hours N")
        hours = a.hours
    else:
        hours = DURATIONS[a.duration]
    if not 0 < a.max_loss <= 100:
        sys.exit("--max-loss must be a percentage between 0 and 100")
    if a.max_loss > s.daily_loss_pct:
        print(f"note: the directive's daily loss limit ({s.daily_loss_pct}%) is stricter than your "
              f"{a.max_loss}% and will stop new entries first.")
    kp = live_gate(s) if a.live else None
    m = Mario(s, "trade", live=a.live, keypair=kp, max_trade_sol=a.max_trade, max_loss_pct=a.max_loss, hours=hours)
    run_engine(m)


def cmd_status(s):
    c = read_control(s.control_path)
    j = Journal(s.db_path)
    if c.get("halted"):
        print(f"HALTED: {c.get('halted_reason')} (resume with: python -m mario resume, or /resume on Telegram)")
    print(f"control: {'PAUSED' if c.get('paused') else 'running'}"
          + (f" (updated {dt.datetime.fromtimestamp(c['updated']):%H:%M:%S})" if c.get("updated") else ""))
    for mode in ("paper", "live"):
        st = j.get(f"risk_state_{mode}")
        if st:
            print(f"{mode}: consecutive losses {st.get('consecutive_losses', 0)}"
                  + (f" · DISABLED ({st.get('disabled_reason')})" if st.get("disabled") else ""))
    for r in j.q("SELECT * FROM positions WHERE status='open'"):
        print(f"open [{r['mode']}] {r['symbol']} {r['mint']} in {r['sol_in']:.4f} SOL, left {r['tokens_left']}")
    day = dt.date.today().isoformat()
    n = j.q("SELECT COUNT(*) n FROM calls WHERE day=?", (day,))[0]["n"]
    print(f"scan calls today: {n} · paper cash {j.get('paper_cash')}")


# ---------------------------------------------------------------- hosted mode (Render etc.)
def env_float(name, default):
    import os
    v = os.environ.get(name, "")
    return float(v) if v.strip() else default


async def wait_for_resume(s, why: str):
    """Halted: stay alive (so the host does not restart us into trading) until /resume."""
    import time
    from .clients.telegram import Telegram
    from .util import Http
    h = Http()
    tg = Telegram(h, s.telegram_bot_token, s.telegram_chat_id, str(Path(s.data_dir) / "telegram_offset"))
    write_control(s.control_path, halted=True, halted_reason=why, stop=False)
    await tg.send(f"⛔ Mario halted: {why}\nSend /resume to start again.")
    print(f"halted: {why} - waiting for /resume (Telegram) or `python -m mario resume`")
    try:
        while True:
            for cmd in await tg.commands():
                if cmd in ("resume", "start"):
                    write_control(s.control_path, halted=False, paused=False)
                elif cmd == "status":
                    await tg.send(f"Mario is halted: {why}. Send /resume to start again.")
            c = read_control(s.control_path)
            if not c.get("halted"):
                await tg.send("▶️ Mario resuming")
                return
            await asyncio.sleep(10)
    finally:
        await h.aclose()


def cmd_service(s):
    """Run forever with settings from environment variables:
    MARIO_MODE=scan|paper|live  MARIO_MAX_TRADE=0.1  MARIO_MAX_LOSS=3  MARIO_HOURS=24
    Trade sessions run back to back. A stop, max-loss or drawdown halt waits for /resume."""
    import os
    mode = os.environ.get("MARIO_MODE", "scan").strip().lower()
    if mode not in ("scan", "paper", "live"):
        sys.exit("MARIO_MODE must be scan, paper or live")
    max_trade, max_loss = env_float("MARIO_MAX_TRADE", 0.1), env_float("MARIO_MAX_LOSS", 3.0)
    hours = env_float("MARIO_HOURS", 24.0)
    kp = None
    if mode == "live":
        rec = reports.paper_track_record(Journal(s.db_path))
        if rec["trades"] < s.min_paper_trades_for_live:
            sys.exit(f"live locked: {rec['trades']}/{s.min_paper_trades_for_live} closed paper trades. "
                     f"Run MARIO_MODE=paper first.")
        if os.environ.get("MARIO_CONFIRM_LIVE", "") != "TRADE LIVE":
            sys.exit("set MARIO_CONFIRM_LIVE='TRADE LIVE' to allow live trading")
        kp = load_keypair(s.wallet_path)
    ephemeral = os.environ.get("MARIO_EPHEMERAL", "").lower() in ("1", "true", "yes")
    if ephemeral and mode == "live":
        sys.exit("live trading needs a persistent disk (risk limits and history must survive restarts). "
                 "Use a paid instance with a disk and remove MARIO_EPHEMERAL.")
    if ephemeral:
        print("NOTE: no persistent disk - the journal, calls and paper record reset whenever the host restarts. "
              "Telegram keeps a copy of every report.")
    if os.environ.get("PORT"):
        from .web import start_web
        start_web(s, int(os.environ["PORT"]), os.environ.get("DASHBOARD_KEY", "").strip())
    print(f"Mario service: mode={mode} max_trade={max_trade} max_loss={max_loss}% hours={hours} data={s.data_dir}")
    if read_control(s.control_path).get("halted"):
        asyncio.run(wait_for_resume(s, read_control(s.control_path).get("halted_reason", "halted earlier")))
    while True:
        if mode == "scan":
            m = Mario(s, "scan")
        else:
            m = Mario(s, "trade", live=(mode == "live"), keypair=kp, max_trade_sol=max_trade,
                      max_loss_pct=max_loss, hours=hours)
        asyncio.run(m.run_until_stopped())
        why = m.shutdown_reason or "engine exited"
        if "SIGTERM" in why:
            return                      # host is stopping us: exit cleanly
        if "duration ended" in why:
            print("session finished - starting the next one")
            continue
        asyncio.run(wait_for_resume(s, why))


def interactive(s):
    print(BANNER)
    pk = wallet_pubkey(s.wallet_path)
    print(f"Trading wallet: {pk or 'none yet'}")
    print("\n  1) Scan mode   - find & call memecoins, hourly + end-of-day reports (no trading)\n"
          "  2) Trade mode  - paper trading (simulated with real quotes)\n"
          "  3) Trade mode  - LIVE trading (real funds)\n"
          "  4) Wallet: create / show balance / export key / withdraw\n"
          "  5) Manage smart wallets to track\n"
          "  6) Status\n  q) Quit")
    ch = input("\nChoose: ").strip().lower()
    if ch == "1":
        run_engine(Mario(s, "scan"))
    elif ch in ("2", "3"):
        mt = float(input("Maximum trade amount (SOL) [0.1]: ") or 0.1)
        ml = float(input("Maximum session loss % [3]: ") or 3)
        d = input("Duration hours: 1, 4, 8, 12, 24 or a custom number [4]: ").strip() or "4"
        ns = argparse.Namespace(max_trade=mt, max_loss=ml, live=(ch == "3"),
                                duration=d if d in DURATIONS else "custom", hours=float(d) if d not in DURATIONS else None)
        cmd_trade(s, ns)
    elif ch == "4":
        w = input("  a) create (new seed phrase)  b) show balance  c) export key & phrase  d) withdraw SOL  "
                  "e) import from seed phrase\n  Choose: ").strip().lower()
        if w in ("a", "e"):
            enc = input("Also lock the wallet file with a passphrase? [y/N] ").strip().lower() == "y"
            cmd_wallet(s, argparse.Namespace(action="new" if w == "a" else "import", encrypt=enc))
        elif w == "b":
            cmd_wallet(s, argparse.Namespace(action="show"))
        elif w == "c":
            cmd_wallet(s, argparse.Namespace(action="export-key"))
        elif w == "d":
            to = input("Destination (blank = your TREASURY_ADDRESS): ").strip() or None
            amt = input("Amount in SOL, or 'all' [all]: ").strip() or "all"
            cmd_wallet(s, argparse.Namespace(action="withdraw", to=to, amount=amt))
    elif ch == "5":
        addr = input("Wallet address to track (blank = list): ").strip()
        if addr:
            cmd_wallets(s, argparse.Namespace(action="add", address=[addr], label=input("Label: ").strip()))
        cmd_wallets(s, argparse.Namespace(action="list", address=[], label=None))
    elif ch == "6":
        cmd_status(s)


def main(argv=None):
    s = load_settings()
    ap = argparse.ArgumentParser(prog="mario", description="Mario - Solana memecoin scanner/trader")
    ap.add_argument("--log", default="INFO")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("scan", help="scan mode: calls + hourly/EOD reports, never trades")
    t = sub.add_parser("trade", help="trade mode (paper by default)")
    t.add_argument("--max-trade", type=float, required=True, help="max SOL per trade")
    t.add_argument("--max-loss", type=float, required=True, help="max session loss %% before Mario stops")
    t.add_argument("--duration", choices=list(DURATIONS) + ["custom"], default="4")
    t.add_argument("--hours", type=float, help="hours when --duration custom")
    t.add_argument("--live", action="store_true", help="use real funds from the trading wallet")
    sub.add_parser("service", help="hosted mode: settings from env vars, runs forever (Render)")
    for c in ("pause", "resume", "stop", "status", "config", "paper-stats"):
        sub.add_parser(c)
    w = sub.add_parser("wallet")
    w.add_argument("action", choices=["new", "import", "show", "export-key", "withdraw"])
    w.add_argument("--encrypt", action="store_true")
    w.add_argument("--to", help="withdraw destination (must be in TREASURY_ADDRESS)")
    w.add_argument("--amount", default="all", help="SOL amount or 'all'")
    ws = sub.add_parser("wallets", help="smart wallets Mario tracks")
    ws.add_argument("action", choices=["add", "list", "remove", "import"])
    ws.add_argument("address", nargs="*")
    ws.add_argument("--label")
    r = sub.add_parser("report")
    r.add_argument("--day", default=dt.date.today().isoformat())
    ra = sub.add_parser("reactivate", help="clear a drawdown/consecutive-loss halt after manual review")
    ra.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)
    setup_logging(a.log, str(Path(s.data_dir) / "mario.log"))

    if a.cmd is None:
        interactive(s)
    elif a.cmd == "scan":
        run_engine(Mario(s, "scan"))
    elif a.cmd == "service":
        cmd_service(s)
    elif a.cmd == "trade":
        cmd_trade(s, a)
    elif a.cmd in ("pause", "resume"):
        write_control(s.control_path, paused=(a.cmd == "pause"), **({"halted": False} if a.cmd == "resume" else {}))
        print(f"{a.cmd}d. Open positions stay protected." if a.cmd == "pause" else "resumed.")
    elif a.cmd == "stop":
        write_control(s.control_path, stop=True)
        print("stop requested: Mario will close open positions, write its report and exit.")
    elif a.cmd == "status":
        cmd_status(s)
    elif a.cmd == "wallet":
        cmd_wallet(s, a)
    elif a.cmd == "wallets":
        cmd_wallets(s, a)
    elif a.cmd == "report":
        text = reports.eod_report(Journal(s.db_path), a.day, s.report_tz)
        print(text)
        print("\nsaved:", reports.save_report(s.reports_dir, f"eod-{a.day}", text))
    elif a.cmd == "reactivate":
        if input("Have you reviewed the recent trades and strategy? Type YES: ").strip() == "YES":
            RiskEngine(s, Journal(s.db_path), "live" if a.live else "paper").reactivate()
            print("reactivated.")
    elif a.cmd == "paper-stats":
        print(json.dumps(reports.paper_track_record(Journal(s.db_path)), indent=2))
    elif a.cmd == "config":
        d = asdict(s)
        for k in list(d):
            if "key" in k or "token" in k:
                d[k] = "***" if d[k] else ""
        print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
