"""Solana JSON-RPC client (only the calls Mario needs)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..util import Http, RateLimiter

LAMPORTS = 1_000_000_000


class RPCError(Exception):
    pass


@dataclass
class MintInfo:
    mint: str
    program: str
    mint_authority: str | None
    freeze_authority: str | None
    decimals: int
    supply: int
    extensions: list[dict] = field(default_factory=list)
    is_initialized: bool = True


class SolanaRPC:
    def __init__(self, http: Http, url: str, rps: float = 8.0):
        self.http = http
        self.url = url
        self.limiter = RateLimiter(rps)
        self._id = 0

    async def call(self, method: str, params: list):
        await self.limiter.wait()
        self._id += 1
        r = await self.http.post(self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if r is None:
            raise RPCError(f"{method}: empty response")
        if "error" in r:
            raise RPCError(f"{method}: {r['error']}")
        return r.get("result")

    async def get_balance(self, pubkey: str) -> int:
        r = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(r["value"])

    async def get_mint_info(self, mint: str) -> MintInfo:
        r = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        v = r and r.get("value")
        if not v:
            raise RPCError("mint account not found")
        data = v.get("data")
        if not isinstance(data, dict) or data.get("parsed", {}).get("type") != "mint":
            raise RPCError("account is not a parsable SPL mint")
        info = data["parsed"]["info"]
        return MintInfo(
            mint=mint,
            program=v.get("owner", ""),
            mint_authority=info.get("mintAuthority"),
            freeze_authority=info.get("freezeAuthority"),
            decimals=int(info.get("decimals", 0)),
            supply=int(info.get("supply", 0)),
            extensions=info.get("extensions") or [],
            is_initialized=bool(info.get("isInitialized", True)),
        )

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int]:
        r = await self.call("getTokenAccountsByOwner",
                            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total, dec = 0, 0
        for acc in (r or {}).get("value", []):
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(ta["amount"])
            dec = int(ta["decimals"])
        return total, dec

    async def simulate(self, tx_b64: str, watch: list[str]) -> dict:
        r = await self.call("simulateTransaction", [tx_b64, {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "processed", "accounts": {"encoding": "base64", "addresses": watch}}])
        return r["value"]

    async def send(self, tx_b64: str) -> str:
        return await self.call("sendTransaction", [tx_b64, {"encoding": "base64", "skipPreflight": False,
                                                            "preflightCommitment": "processed", "maxRetries": 3}])

    async def signature_status(self, sig: str) -> dict | None:
        r = await self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        return (r or {}).get("value", [None])[0]

    async def get_signatures(self, address: str, limit: int = 10, until: str | None = None) -> list[dict]:
        cfg = {"limit": limit, "commitment": "confirmed"}
        if until:
            cfg["until"] = until
        return await self.call("getSignaturesForAddress", [address, cfg]) or []

    async def get_transaction(self, sig: str) -> dict | None:
        return await self.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                         "maxSupportedTransactionVersion": 0}])


STABLES = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def token_deltas_for_owner(tx: dict, owner: str) -> tuple[float, dict[str, float]]:
    """From a jsonParsed getTransaction result -> (sol_delta, {mint: ui_delta}) for `owner`."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return 0.0, {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    sol_delta = 0.0
    if owner in keys:
        i = keys.index(owner)
        sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS

    def bal(lst):
        out: dict[str, float] = {}
        for b in lst or []:
            if b.get("owner") != owner:
                continue
            amt = float(b["uiTokenAmount"].get("uiAmount") or 0)
            out[b["mint"]] = out.get(b["mint"], 0.0) + amt
        return out

    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    deltas = {}
    for m in set(pre) | set(post):
        d = post.get(m, 0.0) - pre.get(m, 0.0)
        if abs(d) > 0:
            deltas[m] = d
    # wrapped SOL counts as SOL
    wsol = deltas.pop("So11111111111111111111111111111111111111112", 0.0)
    return sol_delta + wsol, deltas
