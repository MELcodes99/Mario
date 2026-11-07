"""PumpPortal free data websocket: new Pump.fun launches, graduations (migrations)
and trades by watched wallets. Requires the `websockets` package; without it
Mario falls back to DexScreener polling only."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Awaitable, Callable

try:
    import websockets  # type: ignore
except ImportError:  # pragma: no cover
    websockets = None

log = logging.getLogger("mario.pumpportal")


def ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle: python.org builds on macOS ship without system certificates."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

Handler = Callable[[str, dict], Awaitable[None]]


class PumpPortalStream:
    def __init__(self, url: str, on_event: Handler):
        self.url = url
        self.on_event = on_event
        self.accounts: set[str] = set()
        self._ws = None
        self._stop = False

    @property
    def available(self) -> bool:
        return websockets is not None

    async def watch_accounts(self, accounts: list[str]):
        new = set(accounts) - self.accounts
        self.accounts |= set(accounts)
        if new and self._ws is not None:
            await self._ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": sorted(new)}))

    def stop(self):
        self._stop = True

    async def run(self):
        if websockets is None:
            log.warning("websockets not installed: Pump.fun live stream disabled (pip install websockets)")
            return
        backoff = 2
        while not self._stop:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2 ** 22,
                                              ssl=ssl_context()) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    if self.accounts:
                        await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": sorted(self.accounts)}))
                    log.info("PumpPortal stream connected")
                    backoff = 2
                    async for msg in ws:
                        try:
                            ev = json.loads(msg)
                        except ValueError:
                            continue
                        kind = classify(ev)
                        if kind:
                            await self.on_event(kind, ev)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors -> reconnect
                log.warning("PumpPortal stream dropped (%s); reconnecting in %ss", e, backoff)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def classify(ev: dict) -> str | None:
    tx = ev.get("txType")
    if tx == "create":
        return "new_token"
    if tx == "migrate" or ev.get("pool") in ("pump-amm", "raydium") and "signature" in ev and tx is None:
        return "migration"
    if tx in ("buy", "sell") and ev.get("traderPublicKey"):
        return "trade"
    return None
