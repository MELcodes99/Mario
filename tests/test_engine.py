"""End-to-end flows with fake network clients: scan call and a paper trade."""
import asyncio
import collections
import tempfile
import time

from mario.clients.anthropic_intel import validate_intel
from mario.config import Settings
from mario.engine import Mario
from mario.market import Regime
from mario.risk import Session

from tests import fixtures as fx
from tests.test_core import AI_OK


def make(mode, **kw):
    s = Settings(data_dir=tempfile.mkdtemp())
    m = Mario(s, mode, **kw)
    m.market.regime = Regime(150, 0.4, 2.0, 1.0, 0.5, time.time())
    price = {"r": 1.0}

    async def order(inp, out, amount, taker=None):
        if inp == fx.SOL:     # 1 lamport -> 6 raw tokens at "price" r
            return fx.quote(inp, out, amount, int(amount * 6 / price["r"]))
        return fx.quote(inp, out, amount, int(amount / 6 * price["r"] * 0.995))

    async def mint_info(mint):
        return fx.mint_info()

    async def report(mint):
        return fx.rug()

    async def analyze(mint, data):
        return validate_intel({**AI_OK, "mint_address": mint}, mint), "{}"

    m.jup.order = order
    m.rpc.get_mint_info = mint_info
    m.rug.report = report
    m.intel.api_key = "test"
    m.intel.analyze = analyze
    p = fx.pair()
    m.add_watch(fx.MINT, "migrated", time.time() - 3600)
    it = m.watch[fx.MINT]
    it.pair = p
    q = m.hist.d.setdefault(p.mint, collections.deque())
    for i in range(6):
        q.append((time.time() - 900 + i * 150, p.price_usd * (0.97 + 0.005 * i), p.liquidity_usd, p.mc))
    return m, it, price


def test_scan_makes_a_call():
    m, it, _ = make("scan")
    asyncio.run(m.evaluate_token(it))
    rows = m.j.q("SELECT * FROM calls")
    assert len(rows) == 1, m.j.q("SELECT decision, reason, score_json FROM decisions")
    assert rows[0]["symbol"] == "TFROG" and rows[0]["call_mc"] == 1_500_000
    # a call is never made twice per day
    it.next_eval = 0
    asyncio.run(m.evaluate_token(it))
    assert len(m.j.q("SELECT * FROM calls")) == 1


def test_scan_rejects_mint_authority_permanently():
    m, it, _ = make("scan")

    async def bad(mint):
        return fx.mint_info(mint_authority="Auth1111111111111111111111111111111111111111")
    m.rpc.get_mint_info = bad
    asyncio.run(m.evaluate_token(it))
    assert not m.j.q("SELECT * FROM calls")
    assert fx.MINT not in m.watch and m.rejected.get(fx.MINT)
    d = m.j.q("SELECT decision, reason FROM decisions")[-1]
    assert d["decision"] == "reject" and "mint_authority" in d["reason"]


def test_paper_trade_enters_and_takes_profit():
    m, it, price = make("trade", max_trade_sol=0.5, max_loss_pct=3, hours=1)
    m.session = Session("t", "paper", 0.5, 3, time.time(), time.time() + 3600, 10.0)
    m.risk.session = m.session
    asyncio.run(m.evaluate_token(it))
    assert len(m.pm.open) == 1, m.j.q("SELECT decision, reason FROM decisions")
    pos = next(iter(m.pm.open.values()))
    assert pos.sol_in <= 0.2 + 1e-9          # 2% of 10 SOL cap beats the 0.5 SOL user max
    price["r"] = 1.2
    asyncio.run(m.pm.monitor({}))
    assert pos.tp_hits == [0]
    asyncio.run(m.pm.close_all("session end: test"))
    assert not m.pm.open
    assert m.j.q("SELECT status FROM positions")[0]["status"] == "closed"
