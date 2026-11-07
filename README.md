# 🍄 Mario: Solana memecoin scanner and trader

Mario watches new launches and Pump.fun graduations, tracks smart-money wallets, screens every token through a
strict security gate, scores it out of 100 and then either **calls** it (scan mode) or **trades** it (trade mode).
It follows the MASTER TRADING DIRECTIVE v1.0 throughout. Missing 99 winners is better than buying one rug.

## Install

```bash
cd mario
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add RPC_URL, JUPITER_API_KEY, ANTHROPIC_API_KEY (+ Telegram if wanted)
python -m pytest -q         # 31 offline tests
```

## Use

```bash
python -m mario                       # interactive menu
python -m mario wallet new --encrypt  # dedicated trading wallet (prints address; fund it with a small amount only)
python -m mario wallet show           # address + balance
python -m mario wallets add <addr> --label gmgn1   # smart wallets to track (repeat; or `wallets import file.csv`)

python -m mario scan                  # SCAN: calls only, hourly report + end-of-day ranking
python -m mario trade --max-trade 0.2 --max-loss 3 --duration 4            # paper trading, 4h cycle
python -m mario trade --max-trade 0.2 --max-loss 3 --duration custom --hours 6
python -m mario trade --max-trade 0.2 --max-loss 3 --duration 24 --live    # real funds (locked until validated)

python -m mario pause | resume        # pause = no new calls/entries; open positions stay protected
python -m mario stop                  # close positions, write the report, exit
python -m mario status | report --day 2026-11-07 | paper-stats | config
```

With Telegram configured, reports also go to your phone, and `/pause` `/resume` `/status` `/stop` work from the chat.

## Modes

**Scan mode** never trades. A token gets *called* when it passes every security check, scores at least 85
(`SCAN_CALL_SCORE`) and matches a strategy setup. Mario records the time, market cap, liquidity, score and strategy at
the call, then keeps tracking the market cap.
- **Hourly report:** tokens evaluated, the main rejection reasons, new calls, all of today's calls, and Pump.fun launches and graduations seen.
- **End-of-day report** (at midnight in `REPORT_TZ`): every call of the day, **ranked highest to lowest** by peak
  multiple since the call, with call time, MC at call, peak MC, current MC and current multiple.
  *Peak x is the best the token reached, not a return anyone captured.*

Reports are saved under `~/.mario/reports/<date>/` as Markdown (and JSON for the end-of-day report).

**Trade mode** has three user settings: **max trade amount** (SOL), **max session loss %** and a **duration** of 1, 4, 8, 12 or 24
hours, or a custom number. When the cycle ends, Mario closes every position and writes a session report. The directive's
limits still apply, and the **stricter limit always wins**:
- Each trade risks 0.25% of equity. A position is capped at 2% of equity, total exposure at 6%, and Mario holds at most 3 positions.
- A 3% daily loss stops new entries. A 7% weekly loss does the same. A 10% drawdown halts everything until you run `reactivate`.
- Mario pauses for 1 hour after 3 losses in a row, and disables the strategy after 5.
- Exits: a −12% stop; take-profits selling 25% each at +15%, +30% and +50%; a 15% trailing stop on the last 25%; and emergency exits
  if liquidity is pulled, an authority reappears, a dangerous extension appears or sell routes start failing.

Paper trading uses **real executable Jupiter quotes** with a 0.5% haircut and fees. **Live trading stays locked until there are
100 closed paper trades** (`MIN_PAPER_TRADES_FOR_LIVE`; the directive suggests 500). You must also type `TRADE LIVE` to start.

## Pipeline

1. **Discovery.** Sources: the PumpPortal stream (new launches, graduations, smart-wallet trades), DexScreener latest profiles and boosts, and an RPC poller that reads each tracked wallet's swaps on any DEX.
2. **Security gate**, where *unknown counts as fail*:
   - Mint and freeze authority are read on-chain. Token-2022 extensions are inspected one by one (permanent delegate, hooks, fees, pausable and so on).
   - Liquidity must be at least $50k, the exit side at least $25k, and at least 95% of LP locked or burned. Liquidity is cross-checked between sources and must be stable.
   - Holder limits, excluding pools: top wallet at most 5%, top 10 at most 20%, deployer at most 5%. No insider clusters.
   - RugCheck danger flags are checked, along with a local database of rug deployers.
   - Buy and sell quotes must succeed, round-trip loss must be at most 6%, and price impact at most 2%, measured independently.
   - Manipulation checks cover volume versus liquidity, one-sided order flow and tiny-trade farming.
3. **Score out of 100**: narrative 20 (Anthropic analyst, strict JSON, injection-safe), organic momentum 25, liquidity 20, price structure 20 and market regime 15.
4. **Strategy nomination**: see `STRATEGIES.md`.
5. **Risk engine**, then **execution**. For live trades Mario:
   - gets a fresh Jupiter Swap V2 order;
   - parses the transaction and checks its instructions against an allowlist (no approvals, no transfers out, correct fee payer);
   - simulates it and checks the SOL actually spent;
   - signs it locally, submits it and confirms it.
6. **Journal**: SQLite at `~/.mario/mario.db`. It records every decision, including every rejection, plus fills, MAE/MFE, raw AI responses and wallet grades.

## Wallet safety
- Mario uses its own wallet file: `~/.mario/trading-wallet.json`, chmod 600, AES-GCM encrypted if you choose. Never use your main wallet.
- Fund it only with what Mario may trade. Keep everything else in a separate treasury wallet that Mario never sees.
- Mario has no withdraw function. To move funds, run `wallet export-key`, import the key into Phantom and move them yourself.
- If Jupiter routes through a program Mario doesn't recognise, the trade is rejected and the log shows the program id.
  Add it to `EXTRA_ALLOWED_PROGRAMS` only after checking it.

## Honest expectations
Most memecoins go to zero, and no screener can guarantee a token is safe. An 80% win rate is a research target. Judge Mario on
**net expectancy after fees** over hundreds of paper trades in more than one market regime. The bot is built so that doing nothing
is the default. Run scan mode and paper mode for a long time before you fund the wallet, and only fund it with money you can lose.
This is software, not financial advice.

## Hosting on Render

`render.yaml` defines one always-on **background worker** running `python -m mario service`, plus a 1 GB disk mounted at `/var/data`.
Background workers have no free tier, so this uses the Starter instance. Without the disk, every restart would wipe the journal and reports.

1. Push this folder to a **private** GitHub repo. `.gitignore` keeps `.env`, the wallet and the database out of it.
2. In Render, go to **New → Blueprint**, pick the repo and fill in the prompted secrets: RPC, Jupiter, Anthropic and Telegram.
3. You control Mario from Telegram with `/status` `/report` `/pause` `/resume` `/stop` `/help`. Reports are sent there too.

Choose the mode with env vars in the Render dashboard: `MARIO_MODE` = `scan` | `paper` | `live`, plus `MARIO_MAX_TRADE`, `MARIO_MAX_LOSS` and `MARIO_HOURS`.
- Trade sessions run back to back.
- `/stop`, hitting the session max loss, or a drawdown halt makes Mario **wait for `/resume`** instead of restarting itself.
- For live trading you must also set `MARIO_WALLET_SECRET` (the base58 key from `wallet export-key`) and `MARIO_CONFIRM_LIVE=TRADE LIVE`.
  The paper-trade lock still applies, counted on Render's own disk.
