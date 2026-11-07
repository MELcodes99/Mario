"""Optional Telegram delivery for reports + /pause /resume /status /stop commands."""
from __future__ import annotations

import logging
from pathlib import Path

from ..util import Http

log = logging.getLogger("mario.telegram")


class Telegram:
    def __init__(self, http: Http, token: str, chat_id: str, state_path: str | None = None):
        self.http, self.token, self.chat_id = http, token, str(chat_id)
        self.state_path = Path(state_path) if state_path else None
        self.offset = 0
        if self.state_path and self.state_path.exists():   # never re-run an old /stop after a restart
            try:
                self.offset = int(self.state_path.read_text().strip() or 0)
            except ValueError:
                pass

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str):
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for i in range(0, len(text), 3900):
            try:
                await self.http.post(url, {"chat_id": self.chat_id, "text": text[i:i + 3900],
                                           "disable_web_page_preview": True})
            except Exception as e:
                log.warning("telegram send failed: %s", e)
                return

    async def commands(self) -> list[str]:
        """Commands sent from the configured chat only."""
        if not self.enabled:
            return []
        try:
            r = await self.http.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                    params={"offset": self.offset, "timeout": 0})
        except Exception:
            return []
        out = []
        for u in (r or {}).get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            if self.state_path:
                self.state_path.write_text(str(self.offset))
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                continue
            t = (msg.get("text") or "").strip().lower()
            if t.startswith("/"):
                out.append(t.split()[0][1:].split("@")[0])
        return out
