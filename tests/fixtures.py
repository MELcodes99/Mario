import time

from mario.clients.dexscreener import parse_pair
from mario.clients.jupiter import parse_quote
from mario.clients.rpc import MintInfo
from mario.clients.rugcheck import parse_report
from mario.solana_tx import TOKEN_PROGRAM, TOKEN_2022_PROGRAM

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
PAIR = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"
SOL = "So11111111111111111111111111111111111111112"


def pair_raw(**kw):
    d = {
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": PAIR,
        "baseToken": {"address": MINT, "name": "Test Frog", "symbol": "TFROG"},
        "quoteToken": {"address": SOL, "name": "Wrapped SOL", "symbol": "SOL"},
        "priceNative": "0.000001", "priceUsd": "0.00015",
        "txns": {"m5": {"buys": 40, "sells": 30}, "h1": {"buys": 600, "sells": 400},
                 "h6": {"buys": 2400, "sells": 2000}, "h24": {"buys": 5000, "sells": 4500}},
        "volume": {"m5": 9000, "h1": 120000, "h6": 360000, "h24": 900000},
        "priceChange": {"m5": 1.5, "h1": 14.0, "h6": 30.0, "h24": 60.0},
        "liquidity": {"usd": 160000, "base": 500000000, "quote": 400},
        "fdv": 1500000, "marketCap": 1500000,
        "pairCreatedAt": int((time.time() - 6 * 3600) * 1000),
        "info": {"websites": [{"url": "https://frog.example"}],
                 "socials": [{"type": "twitter", "url": "https://x.com/frog"}]},
    }
    d.update(kw)
    return d


def pair(**kw):
    return parse_pair(pair_raw(**kw))


def mint_info(**kw):
    d = dict(mint=MINT, program=TOKEN_PROGRAM, mint_authority=None, freeze_authority=None,
             decimals=6, supply=10 ** 15, extensions=[])
    d.update(kw)
    return MintInfo(**d)


def rug_raw(**kw):
    d = {
        "mintAuthority": None, "freezeAuthority": None, "tokenProgram": TOKEN_PROGRAM,
        "creator": "Cre8or1111111111111111111111111111111111111", "creatorBalance": 0,
        "token": {"supply": 10 ** 15},
        "risks": [{"name": "Low amount of LP Providers", "level": "warn", "score": 100}],
        "topHolders": [{"address": "PoolVault11111111111111111111111111111111111", "owner": "PoolAuth1111111111111111111111111111111111111", "pct": 22.0, "insider": False}]
                      + [{"address": f"Acc{i}" + "1" * 38, "owner": f"Own{i}" + "1" * 38, "pct": 1.5, "insider": False} for i in range(10)],
        "knownAccounts": {"PoolAuth1111111111111111111111111111111111111": {"name": "PumpSwap", "type": "AMM"}},
        "markets": [{"pubkey": PAIR, "marketType": "pump_amm", "lp": {"lpLockedPct": 100}}],
        "rugged": False, "transferFee": {"pct": 0}, "totalMarketLiquidity": 158000,
        "graphInsidersDetected": 0, "insiderNetworks": None,
    }
    d.update(kw)
    return d


def rug(**kw):
    return parse_report(MINT, rug_raw(**kw))


def quote(inp, out, in_amt, out_amt, impact_frac=0.004, **kw):
    d = {"inputMint": inp, "outputMint": out, "inAmount": str(in_amt), "outAmount": str(out_amt),
         "otherAmountThreshold": str(int(out_amt * 0.99)), "priceImpactPct": str(impact_frac),
         "slippageBps": 100, "feeBps": 5}
    d.update(kw)
    return parse_quote(d)
