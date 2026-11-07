"""Mario key & connection check:  python check_keys.py
Tests every service Mario uses with your .env settings. Spends nothing except a
tiny Anthropic call (a few tokens)."""
import asyncio
import json
import time

from mario.clients.dexscreener import DexScreener
from mario.clients.jupiter import SOL_MINT, Jupiter
from mario.clients.rpc import LAMPORTS, SolanaRPC
from mario.clients.rugcheck import RugCheck
from mario.clients.telegram import Telegram
from mario.config import load_settings
from mario.util import Http
from mario.wallet import wallet_pubkey

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
results = []


def mask(v):
    return f"{v[:4]}…{v[-4:]}" if v and len(v) > 10 else ("set" if v else "NOT SET")


async def check(name, coro):
    t = time.time()
    try:
        detail = await asyncio.wait_for(coro, 30)
        results.append((True, name, detail, time.time() - t))
    except Exception as e:
        results.append((False, name, f"{type(e).__name__}: {(str(e) or repr(e.__cause__ or e))[:220]}", time.time() - t))


async def main():
    s = load_settings()
    h = Http()
    print("\nSettings in use:")
    print(f"  RPC_URL            {s.rpc_url.split('?')[0]}{'?api-key=' + mask(s.rpc_url.split('api-key=')[-1]) if 'api-key=' in s.rpc_url else ''}")
    print(f"  JUPITER_API_KEY    {mask(s.jupiter_api_key)}")
    print(f"  ANTHROPIC_API_KEY  {mask(s.anthropic_api_key)}  model={s.anthropic_model}  web_search={s.anthropic_web_search}")
    print(f"  RUGCHECK_API_KEY   {mask(s.rugcheck_api_key)} (optional)")
    print(f"  TELEGRAM           {'set' if s.telegram_bot_token and s.telegram_chat_id else 'not set (optional)'}\n")

    rpc = SolanaRPC(h, s.rpc_url)

    async def t_rpc():
        slot = await rpc.call("getSlot", [])
        pk = wallet_pubkey(s.wallet_path)
        bal = f", trading wallet {await rpc.get_balance(pk) / LAMPORTS:.4f} SOL" if pk else ", no trading wallet yet"
        mi = await rpc.get_mint_info(BONK)
        return f"slot {slot}{bal}, mint parse ok ({'no' if not mi.mint_authority else 'has'} mint authority on BONK)"

    async def t_jup():
        q = await Jupiter(h, s.jupiter_base, s.jupiter_api_key, 2).order(SOL_MINT, USDC, int(0.01 * LAMPORTS))
        fields = [k for k in ("priceImpactPct", "priceImpact", "otherAmountThreshold", "requestId") if k in q.raw]
        warn = "" if ("priceImpactPct" in q.raw or "priceImpact" in q.raw) else "  ⚠️ no price-impact field: send me this output"
        return f"0.01 SOL -> {q.out_amount / 1e6:.4f} USDC, impact {q.price_impact_pct:.3f}%, fields {fields}{warn}"

    async def t_dex():
        p = (await DexScreener(h, s.dexscreener_base).tokens([SOL_MINT])).get(SOL_MINT)
        return f"SOL ${p.price_usd:.2f}, h1 {p.chg('h1'):+.2f}%"

    async def t_rug():
        r = await RugCheck(h, s.rugcheck_base, s.rugcheck_api_key).report(BONK)
        return f"BONK report: {len(r.top_holders)} holders, {len(r.markets)} markets, {len(r.risks)} risks"

    async def t_ai():
        if not s.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        body = {"model": s.anthropic_model, "max_tokens": 20,
                "messages": [{"role": "user", "content": "Reply with exactly: MARIO OK"}]}
        if s.anthropic_web_search:
            body["tools"] = [{"type": s.anthropic_web_search_tool, "name": "web_search", "max_uses": 1}]
        r = await h.post("https://api.anthropic.com/v1/messages", body,
                         headers={"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()
        return f"model replied {text!r}" + (" (web search tool accepted)" if s.anthropic_web_search else "")

    async def t_pump():
        import websockets
        from mario.clients.pumpportal import ssl_context
        async with websockets.connect(s.pumpportal_ws, ssl=ssl_context()) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if ev.get("mint"):
                    return f"live event received: txType={ev.get('txType')} keys={sorted(ev)[:8]}"
            return "connected (no token event yet)"

    async def t_tg():
        tg = Telegram(h, s.telegram_bot_token, s.telegram_chat_id)
        if not tg.enabled:
            return "skipped (not configured)"
        me = await h.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe")
        await h.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                     {"chat_id": s.telegram_chat_id, "text": "🍄 Mario key check: Telegram works"})
        return f"bot @{me['result']['username']} sent you a test message"

    for name, fn in (("Solana RPC", t_rpc), ("Jupiter", t_jup), ("DexScreener", t_dex), ("RugCheck", t_rug),
                     ("Anthropic", t_ai), ("PumpPortal stream", t_pump), ("Telegram", t_tg)):
        print(f"checking {name}…", flush=True)
        await check(name, fn())
    await h.aclose()

    print("\nResults:")
    for ok, name, detail, dt in results:
        print(f"  {'✅' if ok else '❌'} {name:18} {detail}  ({dt:.1f}s)")
    bad = [n for ok, n, _, _ in results if not ok]
    print("\nAll good - you can run scan mode." if not bad else f"\nFix: {', '.join(bad)}. Paste this output to Claude if unsure.")


asyncio.run(main())
