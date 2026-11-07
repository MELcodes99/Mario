"""Shared helpers: async HTTP (httpx if installed, stdlib fallback), rate limiting,
base58, compact-u16, small TTL cache, logging."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # optional, faster
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None

log = logging.getLogger("mario")


def setup_logging(level: str = "INFO", logfile: str | None = None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger("mario")
    root.setLevel(level)
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    root.addHandler(h)
    if logfile:
        fh = logging.FileHandler(logfile)
        fh.setFormatter(fmt)
        root.addHandler(fh)


# --------------------------------------------------------------------------- HTTP
class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


UA = "mario-trading-bot/1.0"


class Http:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout) if httpx else None

    async def request(self, method: str, url: str, params: dict | None = None,
                      json_body=None, headers: dict | None = None):
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = url + ("&" if "?" in url else "?") + q
        h = {"User-Agent": UA, "Accept": "application/json"}
        if headers:
            h.update(headers)
        if self._client is not None:
            r = await self._client.request(method, url, json=json_body, headers=h)
            if r.status_code >= 400:
                raise HttpError(r.status_code, r.text)
            return r.json() if r.content else None
        return await asyncio.to_thread(self._urllib, method, url, json_body, h)

    def _urllib(self, method, url, json_body, headers):
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, e.read()[:1000].decode(errors="replace"))
        return json.loads(body) if body else None

    async def get(self, url, params=None, headers=None):
        return await self.request("GET", url, params=params, headers=headers)

    async def post(self, url, json_body=None, headers=None):
        return await self.request("POST", url, json_body=json_body, headers=headers)

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()


class RateLimiter:
    """Minimum spacing between calls (rps)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._d: dict = {}

    def get(self, k):
        v = self._d.get(k)
        if not v:
            return None
        ts, val = v
        if time.time() - ts > self.ttl:
            self._d.pop(k, None)
            return None
        return val

    def set(self, k, val):
        self._d[k] = (time.time(), val)

    def drop(self, k):
        self._d.pop(k, None)


# --------------------------------------------------------------------------- base58
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_IDX:
            raise ValueError(f"invalid base58 char {c!r}")
        n = n * 58 + _B58_IDX[c]
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + full


def is_pubkey(s: str) -> bool:
    try:
        return isinstance(s, str) and 32 <= len(s) <= 44 and len(b58decode(s)) == 32
    except ValueError:
        return False


# --------------------------------------------------------------------------- compact-u16
def read_compact_u16(buf: bytes, off: int) -> tuple[int, int]:
    val, shift = 0, 0
    for i in range(3):
        b = buf[off + i]
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off + i + 1
        shift += 7
    raise ValueError("bad compact-u16")


def write_compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def fnum(x, digits=2) -> str:
    """Human number: 1234567 -> 1.23M"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"{x / div:.{digits}f}{unit}"
    return f"{x:.{digits}f}"


def now() -> float:
    return time.time()
