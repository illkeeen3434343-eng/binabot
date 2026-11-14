"""Error bookkeeping + admin notifications. Admins hear about PROBLEMS only, and the
same problem is not repeated more often than ALERT cool-down allows."""
from __future__ import annotations

import html
import logging
import time

log = logging.getLogger("alerts")


class Alerts:
    def __init__(self, db, cfg, send_admin):
        self.db, self.cfg, self._send = db, cfg, send_admin   # send_admin(text) -> awaitable
        self._last: dict[tuple, float] = {}

    async def error(self, source: str, code: str, message: str):
        log.error("%s [%s] %s", source, code, message)
        await self.db.add_error(source, code, message)
        key = (source, str(code))
        if time.time() - self._last.get(key, 0) < self.cfg.alert_cooldown_min * 60:
            return
        self._last[key] = time.time()
        await self._safe(f"⚠️ <b>Xəta</b> — {html.escape(source)}\n"
                         f"Kod: <code>{html.escape(str(code))}</code>\n{html.escape(str(message)[:600])}")

    async def recovered(self, source: str):
        self._last = {k: v for k, v in self._last.items() if k[0] != source}
        await self._safe(f"✅ <b>{html.escape(source)}</b> yenidən işləyir.")

    async def _safe(self, text):
        try:
            await self._send(text)
        except Exception:                      # alerting must never break the caller
            log.exception("admin alert could not be sent")
