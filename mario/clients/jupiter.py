"""Jupiter Swap API V2 (GET /order + POST /execute).

Jupiter is the router only. Directive §9: it is never treated as a security auditor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..util import Http, HttpError, RateLimiter

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    min_out_amount: int
    price_impact_pct: float      # positive percent, e.g. 0.8 == 0.8%
    slippage_bps: int
    fee_bps: int
    transaction: str | None      # base64, only when taker was given
    request_id: str | None
    router: str
    fetched_at: float
    raw: dict

    @property
    def implied_slippage_pct(self) -> float:
        if not self.out_amount:
            return 100.0
        return max(0.0, 100.0 * (1 - self.min_out_amount / self.out_amount))

    @property
    def age_s(self) -> float:
        return time.time() - self.fetched_at


def _i(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def parse_quote(r: dict) -> Quote:
    if r.get("errorCode") or r.get("error"):
        raise ValueError(f"jupiter: {r.get('errorMessage') or r.get('error')}")
    impact = None
    if r.get("priceImpactPct") not in (None, ""):
        impact = abs(float(r["priceImpactPct"])) * 100.0     # fraction -> percent
    elif r.get("priceImpact") not in (None, ""):
        impact = abs(float(r["priceImpact"]))               # already percent
    out = _i(r.get("outAmount"))
    return Quote(
        input_mint=r.get("inputMint", ""), output_mint=r.get("outputMint", ""),
        in_amount=_i(r.get("inAmount")), out_amount=out,
        min_out_amount=_i(r.get("otherAmountThreshold")) or out,
        price_impact_pct=impact if impact is not None else 100.0,
        slippage_bps=_i(r.get("slippageBps")), fee_bps=_i(r.get("feeBps")),
        transaction=r.get("transaction") or None, request_id=r.get("requestId"),
        router=str(r.get("router") or r.get("swapType") or ""), fetched_at=time.time(), raw=r,
    )


class Jupiter:
    def __init__(self, http: Http, base: str, api_key: str = "", rps: float = 0.5):
        self.http, self.base = http, base.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else None
        self.limiter = RateLimiter(rps)

    async def order(self, input_mint: str, output_mint: str, amount: int, taker: str | None = None) -> Quote:
        if amount <= 0:
            raise ValueError("amount must be positive")
        await self.limiter.wait()
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount)}
        if taker:
            params["taker"] = taker
        try:
            r = await self.http.get(f"{self.base}/order", params=params, headers=self.headers)
        except HttpError as e:
            raise ValueError(f"jupiter order failed: {e}") from e
        q = parse_quote(r or {})
        if q.out_amount <= 0:
            raise ValueError("jupiter returned no route")
        return q

    async def execute(self, signed_tx_b64: str, request_id: str) -> dict:
        await self.limiter.wait()
        return await self.http.post(f"{self.base}/execute",
                                    {"signedTransaction": signed_tx_b64, "requestId": request_id},
                                    headers=self.headers) or {}
