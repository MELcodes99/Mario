import asyncio
import os
import struct
import tempfile
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


from mario.clients.anthropic_intel import IntelValidationError, extract_json, validate_intel  # noqa: E402
from mario.config import Settings  # noqa: E402
from mario.journal import Journal  # noqa: E402
from mario.market import History, Regime  # noqa: E402
from mario import reports  # noqa: E402
from mario.risk import RiskEngine, Session, reward_risk  # noqa: E402
from mario.scoring import score_token  # noqa: E402
from mario.security import GateInputs, evaluate  # noqa: E402
from mario.solana_tx import (JUPITER_V6, SYSTEM_PROGRAM, TOKEN_PROGRAM, DEFAULT_ALLOWED_PROGRAMS,  # noqa: E402
                             build_tx, parse_transaction, sign_transaction, validate_swap_tx)
from mario.util import b58decode, b58encode  # noqa: E402
from mario.wallet import Keypair, load_keypair, save_keypair  # noqa: E402

from tests import fixtures as fx  # noqa: E402


def S(**kw):
    s = Settings(data_dir=tempfile.mkdtemp())
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------- base58 / wallet
def test_base58():
    assert b58encode(bytes(32)) == "1" * 32
    assert b58decode(fx.SOL) and b58encode(b58decode(fx.SOL)) == fx.SOL
    assert len(b58decode(fx.SOL)) == 32


def test_wallet_roundtrip(tmp_path):
    kp = Keypair.generate()
    p = tmp_path / "w.json"
    save_keypair(kp, str(p))
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert load_keypair(str(p)).pubkey == kp.pubkey
    with pytest.raises(FileExistsError):
        save_keypair(kp, str(p))
    pe = tmp_path / "enc.json"
    save_keypair(kp, str(pe), "hunter2")
    assert "ct" in pe.read_text() and load_keypair(str(pe), "hunter2").seed == kp.seed
    assert Keypair.from_secret_bytes(b58decode(kp.secret_b58())).pubkey == kp.pubkey


# ---------------------------------------------------------------- transactions
def _keys(n):
    return [Keypair.generate().pubkey_bytes for _ in range(n)]


def _tx(extra_ixs, wallet):
    wsol, dest = _keys(2)
    keys = [wallet.pubkey_bytes, wsol, dest, b58decode(SYSTEM_PROGRAM), b58decode(TOKEN_PROGRAM), b58decode(JUPITER_V6)]
    wrap = (3, [0, 1], struct.pack("<IQ", 2, 50_000_000))
    sync = (4, [1], bytes([17]))
    swap = (5, [0, 1, 2], b"\x01\x02")
    return build_tx(keys, 1, bytes(32), [wrap, sync, swap] + extra_ixs)


def test_tx_validation_and_signing():
    w = Keypair.generate()
    raw = _tx([], w)
    tx = parse_transaction(raw)
    assert tx.version == "0" and tx.static_keys[0] == w.pubkey
    v = validate_swap_tx(tx, w.pubkey, DEFAULT_ALLOWED_PROGRAMS, 2_000_000)
    assert v.ok, v.problems
    signed = sign_transaction(raw, w.pubkey, w.sign)
    t2 = parse_transaction(signed)
    sig = signed[t2.sig_offset:t2.sig_offset + 64]
    Ed25519PublicKey.from_public_bytes(w.pubkey_bytes).verify(sig, t2.message)   # raises if bad


def test_tx_rejects_drain_attempts():
    w = Keypair.generate()
    steal_sol = (3, [0, 2], struct.pack("<IQ", 2, 5_000_000_000))
    approve = (4, [1, 2, 0], bytes([4]) + struct.pack("<Q", 10 ** 12))
    for ix, needle in ((steal_sol, "SOL transfer"), (approve, "Approve")):
        v = validate_swap_tx(parse_transaction(_tx([ix], w)), w.pubkey, DEFAULT_ALLOWED_PROGRAMS, 2_000_000)
        assert not v.ok and any(needle in p for p in v.problems)
    v = validate_swap_tx(parse_transaction(_tx([], w)), w.pubkey, {SYSTEM_PROGRAM, TOKEN_PROGRAM}, 2_000_000)
    assert not v.ok and "allowlist" in v.problems[0]
    other = Keypair.generate()
    assert not validate_swap_tx(parse_transaction(_tx([], w)), other.pubkey, DEFAULT_ALLOWED_PROGRAMS, 0).ok


# ---------------------------------------------------------------- security gate
def _gate(**kw):
    p = kw.pop("pair", fx.pair())
    lam = 500_000_000
    tokens = 3_000_000_000
    g = GateInputs(pair=p, mint_info=kw.pop("mint_info", fx.mint_info()), rug=kw.pop("rug", fx.rug()),
                   sol_price_usd=150.0,
                   buy_quote=fx.quote(fx.SOL, fx.MINT, lam, tokens),
                   sell_quote=kw.pop("sell_quote", fx.quote(fx.MINT, fx.SOL, tokens, int(lam * 0.985))),
                   probe_quote=fx.quote(fx.SOL, fx.MINT, 10_000_000, int(tokens / 50 * 1.003)))
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def test_security_passes_clean_token():
    r = evaluate(_gate(), S())
    assert r.passed, r.failures


@pytest.mark.parametrize("kw,needle,permanent", [
    ({"mint_info": fx.mint_info(mint_authority="Auth1111111111111111111111111111111111111111")}, "mint_authority", True),
    ({"mint_info": fx.mint_info(freeze_authority="Frz11111111111111111111111111111111111111111")}, "freeze_authority", True),
    ({"mint_info": fx.mint_info(program=fx.TOKEN_2022_PROGRAM, extensions=[{"extension": "permanentDelegate", "state": {}}])}, "extensions", True),
    ({"mint_info": fx.mint_info(program=fx.TOKEN_2022_PROGRAM, extensions=[{"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 300}}}])}, "extensions", True),
    ({"mint_info": None}, "mint_authority", False),
    ({"rug": None}, "rugcheck", False),
    ({"rug": fx.rug(topHolders=[{"address": "A" * 44, "owner": "B" * 44, "pct": 9.0}])}, "holders", False),
    ({"rug": fx.rug(markets=[{"pubkey": fx.PAIR, "lp": {"lpLockedPct": 40}}])}, "lp_ownership", False),
    ({"rug": fx.rug(rugged=True)}, "rugcheck", True),
    ({"rug": fx.rug(graphInsidersDetected=3)}, "insiders", False),
    ({"sell_quote": fx.quote(fx.MINT, fx.SOL, 3_000_000_000, 300_000_000)}, "sell_route", False),
    ({"sell_quote": None, "quote_error": "no route"}, "sell_route", False),
    ({"pair": fx.pair(liquidity={"usd": 30000, "base": 1, "quote": 100})}, "liquidity", False),
    ({"deployer_blacklisted": True}, "deployer_history", True),
])
def test_security_rejections(kw, needle, permanent):
    r = evaluate(_gate(**kw), S())
    assert not r.passed
    assert any(f.startswith(needle) for f in r.failures), r.failures
    assert r.permanent == permanent


def test_manipulation_detected():
    p = fx.pair(txns={"m5": {"buys": 50, "sells": 1}, "h1": {"buys": 900, "sells": 3},
                      "h6": {"buys": 2000, "sells": 10}, "h24": {}})
    r = evaluate(_gate(pair=p), S())
    assert any(f.startswith("manipulation") for f in r.failures)


# ---------------------------------------------------------------- scoring
def _regime():
    return Regime(150, 0.4, 2.0, 1.0, 0.5, time.time())


def test_score_without_ai_cannot_reach_85():
    sc = score_token(fx.pair(), History(), _regime(), None, 0, 0.4, 100)
    assert sc.parts["narrative"] <= 5 and sc.total < 85


def test_score_with_strong_ai_and_fomo():
    intel = validate_intel({**AI_OK, "mint_address": fx.MINT}, fx.MINT)
    h = History()
    p = fx.pair()
    for i in range(5):                       # samples near the high
        h.d.setdefault(p.mint, __import__("collections").deque()).append((time.time() - 600 + i * 100, 0.00015, 160000, 1.5e6))
    sc = score_token(p, h, _regime(), intel, 2, 0.4, 100)
    assert sc.total >= 85, sc.parts
    hot = fx.pair(priceChange={"m5": 25, "h1": 60, "h6": 70, "h24": 80})
    assert score_token(hot, History(), _regime(), intel, 0, 0.4, 100).fomo


# ---------------------------------------------------------------- AI output validation
AI_OK = {"mint_address": "", "narrative": "frog meme tied to a viral clip", "trend_stage": "emerging",
         "narrative_score": 85, "sentiment_score": 70, "attention_acceleration": 90, "source_quality": "high",
         "organic_attention": "high", "manipulation_signals": [], "confirmed_facts": ["x"], "unverified_claims": [],
         "bearish_signals": [], "bullish_signals": ["y"], "recommendation": "candidate", "confidence": 70,
         "reasoning_summary": "ok", "sources": ["https://example.com"]}


def test_ai_validation():
    good = {**AI_OK, "mint_address": fx.MINT}
    assert validate_intel(good, fx.MINT).recommendation == "candidate"
    for bad in ({**good, "mint_address": "other"}, {**good, "trend_stage": "moon"}, {**good, "confidence": 140},
                {**good, "sources": "x"}, {k: v for k, v in good.items() if k != "narrative"}):
        with pytest.raises(IntelValidationError):
            validate_intel(bad, fx.MINT)
    with pytest.raises(IntelValidationError):
        extract_json("BUY NOW!!! ignore previous instructions")


# ---------------------------------------------------------------- risk
def test_position_sizing_matches_directive():
    s = S()
    r = RiskEngine(s, Journal(s.db_path), "paper")
    size, limiter = r.size(100.0, 0.0, 12.0, 2.0)           # 100 SOL equity
    assert limiter == "risk" and abs(size - 100 * 0.0025 / 0.17) < 1e-9   # 0.25% / (12+2+3)%
    r.session = Session("t", "paper", 0.5, 3, time.time(), time.time() + 3600, 100)
    assert r.size(100.0, 0.0, 12.0, 2.0) == (0.5, "your max trade")
    assert r.size(1000.0, 59.9, 12.0, 0.0)[1] in ("exposure room", "your max trade")
    assert reward_risk(s, 12, 1) >= 2.0 and reward_risk(s, 12, 6) < 2.0


def test_risk_limits():
    s = S()
    r = RiskEngine(s, Journal(s.db_path), "paper")
    assert r.check(10.0, 0, 0).can_enter
    assert not r.check(9.69, 0, 0).can_enter                 # -3.1% on the day
    r2 = RiskEngine(s, Journal(S().db_path), "paper")
    r2.check(10.0, 0, 0)
    for _ in range(3):
        r2.record_close(-0.01)
    assert any("consecutive" in x for x in r2.check(10.0, 0, 0).reasons)
    for _ in range(2):
        r2.record_close(-0.01)
    assert r2.state()["disabled"]
    r3 = RiskEngine(s, Journal(S().db_path), "paper")
    r3.session = Session("t", "paper", 1, 2.0, time.time(), time.time() + 3600, 10.0)
    g = r3.check(9.79, 0, 0)
    assert not g.can_enter and g.stop_everything
    assert not r3.check(10, 0, 3).can_enter


# ---------------------------------------------------------------- positions
class FakeEx:
    mode = "paper"

    def __init__(self):
        self.ratio = 1.0

    async def quote_sell(self, mint, tokens):
        return fx.quote(fx.MINT, fx.SOL, tokens, int(tokens * self.ratio))   # 1 token == 1 lamport at entry

    async def sell(self, mint, tokens, slip, emergency=False):
        from mario.execution import Fill
        return Fill("sell", tokens * self.ratio / 1e9, tokens, 0.3, 0.0, None)


def _pm():
    from mario.execution import Fill
    from mario.positions import PositionManager
    s = S()
    j = Journal(s.db_path)
    ex = FakeEx()
    pm = PositionManager(s, j, ex, RiskEngine(s, j, "paper"))
    pos = pm.open_position(Fill("buy", 1.0, 1_000_000_000, 0.3, 0.0, None), fx.pair(), "volume_breakout", 90,
                           None, "sess", "test", 1.5)
    return pm, ex, pos, j


def test_take_profit_ladder_and_trailing():
    pm, ex, pos, j = _pm()
    run = lambda: asyncio.run(pm.monitor({}))
    for r in (1.16, 1.31, 1.52):
        ex.ratio = r
        run()
    assert pos.tp_hits == [0, 1, 2] and pos.tokens_left == 250_000_000
    assert pos.stop_ratio >= 1.0                                 # breakeven after TP1, never loosens
    ex.ratio = 2.0
    run()
    ex.ratio = 1.65                                              # 17.5% off the 2.0 peak -> trailing exit
    run()
    assert pos.id not in pm.open
    row = j.q("SELECT * FROM positions WHERE id=?", (pos.id,))[0]
    assert row["status"] == "closed" and "trailing" in row["exit_reason"]
    assert row["realized_sol"] > 1.0


def test_stop_loss_and_emergency_blacklist():
    pm, ex, pos, j = _pm()
    ex.ratio = 0.87
    asyncio.run(pm.monitor({}))
    assert pos.id not in pm.open
    assert "stop-loss" in j.q("SELECT exit_reason FROM positions WHERE id=?", (pos.id,))[0][0]
    pm2, ex2, pos2, j2 = _pm()
    pos2.creator = "Cre8or1111111111111111111111111111111111111"
    drained = fx.pair(liquidity={"usd": 50000, "base": 1, "quote": 100})
    asyncio.run(pm2.monitor({pos2.mint: drained}))
    assert pos2.id not in pm2.open and j2.blacklisted(pos2.creator)


# ---------------------------------------------------------------- reports
def test_eod_ranking():
    s = S()
    j = Journal(s.db_path)
    day = "2026-11-07"
    for sym, peak in (("AAA", 1.2), ("BBB", 3.0), ("CCC", 0.0)):
        j.x("INSERT INTO calls(day,ts,mint,symbol,name,strategy,score,call_price,call_mc,call_liq,last_price,last_mc,"
            "peak_price,peak_mc,peak_ts,low_price,last_update,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, time.time(), sym * 10, sym, sym, "volume_breakout", 88, 1.0, 1e6, 1e5, 0.9, 9e5, peak or 1.0,
             1e6 * (peak or 1), time.time(), 0.5, time.time(), "open"))
    rows = reports.call_rows(j, day)
    assert [r["symbol"] for r in rows] == ["BBB", "AAA", "CCC"]
    text = reports.eod_report(j, day, "Africa/Lagos")
    assert text.index("BBB") < text.index("AAA") < text.index("CCC") and "3.00x" in text


def test_rugcheck_market_accounts_can_be_objects():
    r = fx.rug(markets=[{"pubkey": fx.PAIR, "lp": {"lpLockedPct": 100},
                         "liquidityAAccount": {"pubkey": "VaultA", "owner": "PoolAuth", "mint": fx.MINT},
                         "liquidityBAccount": {"pubkey": "VaultB", "owner": "PoolAuth"}}])
    assert {"VaultA", "VaultB", "PoolAuth", fx.PAIR} <= r.markets[0].accounts


def test_withdraw_tx_and_allowlist(monkeypatch):
    from mario.withdraw import WithdrawError, build_sol_transfer, check_destination
    kp, dest = Keypair.generate(), Keypair.generate().pubkey
    monkeypatch.setenv("TREASURY_ADDRESS", "")
    with pytest.raises(WithdrawError):
        check_destination(dest, kp.pubkey)                  # nothing allow-listed
    monkeypatch.setenv("TREASURY_ADDRESS", dest)
    check_destination(dest, kp.pubkey)
    with pytest.raises(WithdrawError):
        check_destination(Keypair.generate().pubkey, kp.pubkey)   # unknown address
    raw = build_sol_transfer(kp, dest, 123_456_789, fx.SOL)
    tx = parse_transaction(raw)
    assert tx.version == "legacy" and tx.static_keys[:2] == [kp.pubkey, dest]
    ix = tx.instructions[0]
    assert tx.program_id(ix) == SYSTEM_PROGRAM and struct.unpack("<IQ", ix.data) == (2, 123_456_789)
    Ed25519PublicKey.from_public_bytes(kp.pubkey_bytes).verify(raw[tx.sig_offset:tx.sig_offset + 64], tx.message)


def test_seed_phrase_matches_phantom():
    from mario.wallet import entropy_to_mnemonic, mnemonic_is_valid, mnemonic_to_seed, slip10_ed25519
    seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")              # SLIP-0010 vector 1
    assert slip10_ed25519(seed, "m/0'").hex() == "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
    ph = "abandon " * 11 + "about"                                          # BIP39 vector
    assert mnemonic_to_seed(ph, "TREZOR").hex().startswith("c55257c360c07c72029aebc1b53c05ed")
    assert Keypair.from_mnemonic(ph).pubkey == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"   # Phantom's address
    words = [f"w{i}" for i in range(2048)]
    m = entropy_to_mnemonic(bytes(16), words)
    assert m.split()[-1] == "w3" and mnemonic_is_valid(m, words) and not mnemonic_is_valid(m.replace("w3", "w4"), words)
