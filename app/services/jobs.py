"""Time-based housekeeping. `tick()` is idempotent and restart-safe: every job
remembers in the database (kv / user row) the day it last ran."""
from __future__ import annotations

import datetime as dt
import html
import logging
import time
from zoneinfo import ZoneInfo

from ..plans import ST_ACTIVE, ST_EXPIRED
from ..texts import EXPIRED, STATUS, expiry_reminder, fmt_dt

log = logging.getLogger("jobs")
DAY = 86400


class Jobs:
    def __init__(self, db, cfg, messenger, monitor=None, clock=time.time):
        self.db, self.cfg, self.msg, self.monitor, self.clock = db, cfg, messenger, monitor, clock

    def _local(self, ts) -> dt.datetime:
        return dt.datetime.fromtimestamp(ts, ZoneInfo(self.cfg.timezone))

    async def tick(self):
        now = int(self.clock())
        local = self._local(now)
        today = local.strftime("%Y-%m-%d")
        await self.expire_users(now)
        await self.db.expire_queue(now - int(self.cfg.queue_ttl_hours * 3600))
        if await self.db.kv_get("queue_day") != today:                 # first tick of a new day
            if self.cfg.queue_reset_at_midnight and await self.db.kv_get("queue_day"):
                midnight = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
                n = await self.db.expire_queue(midnight)
                log.info("midnight: %s leftover queued ads dropped", n)
            await self.db.kv_set("queue_day", today)
        if local.hour >= self.cfg.reminder_hour:
            await self.remind(now, today)
        if local.hour >= self.cfg.report_hour and await self.db.kv_get("report_day") != today:
            await self.db.kv_set("report_day", today)
            await self.msg.admins(await self.report(now))
            await self.db.prune()

    async def expire_users(self, now):
        for u in await self.db.due_expired(now):
            await self.db.set_status(u["user_id"], ST_EXPIRED)
            await self.msg.user(u["user_id"], EXPIRED)
            log.info("user %s expired", u["user_id"])

    async def remind(self, now, today):
        """Last 3 days before expiry: one reminder per day, at REMINDER_HOUR."""
        for u in await self.db.expiring_between(now, now + 3 * DAY):
            if u["last_reminder_day"] == today:
                continue
            days = max(1, -(-(u["expires_at"] - now) // DAY))          # ceil
            await self.db.set_user(u["user_id"], last_reminder_day=today)
            await self.msg.user(u["user_id"], expiry_reminder(days, u["expires_at"], self.cfg.timezone))

    async def report(self, now) -> str:
        since = now - DAY
        tz = self.cfg.timezone
        sc = await self.db.status_counts()
        ds = await self.db.delivery_stats(since)
        cs = await self.db.cycle_stats(since)
        errs = await self.db.error_summary(since)
        last = await self.db.last_cycle()
        lines = [f"🗒 <b>Gündəlik hesabat</b> — {fmt_dt(now, tz)}", "",
                 "<b>İstifadəçilər</b>",
                 f"Cəmi: {sum(sc.values())} | " + ", ".join(
                     f"{STATUS.get(k, k)}: {v}" for k, v in sorted(sc.items())),
                 f"Gözləyən sorğular: {len(await self.db.pending_requests())}", "",
                 "<b>Son 24 saat</b>",
                 f"Monitorinq dövrləri: {cs['n']} (problemli: {cs['bad'] or 0})",
                 f"Axtarış sorğuları: uğurlu {cs['ok']}, uğursuz {cs['failed']}",
                 f"Tapılan yeni elan (növbəyə): {cs['new_ads']}",
                 f"Göndərilən: {ds.get('sent', 0)} | Növbədə: {await self.db.count_queued()} | "
                 f"Vaxtı keçən: {ds.get('expired', 0)} | Silinmiş elan: {ds.get('gone', 0)} | "
                 f"Uğursuz: {ds.get('failed', 0)}",
                 f"Son dövr: {fmt_dt(last['started_at'], tz) if last else '—'}"]
        if self.monitor:
            until = await self.monitor.blocked_until()
            if until > now:
                lines.append(f"⛔️ Bina.az fasiləsi: {fmt_dt(until, tz)}-dək")
        lines += ["", "<b>Xətalar</b>"]
        if errs:
            lines += [f"• {html.escape(e['source'])} [{html.escape(e['code'] or '')}] × {e['n']} "
                      f"(son: {fmt_dt(e['last'], tz)})" for e in errs[:10]]
        else:
            lines.append("Xəta qeydə alınmayıb ✅")
        return "\n".join(lines)
