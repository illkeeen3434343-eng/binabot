"""Delivery queue -> users, under a ROLLING one-hour limit per subscription plan.

The limit is computed from persisted 'sent' rows (sent_at > now-3600), so it is
exact across restarts. Over-limit ads stay queued and leave as capacity frees up.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..bina.parser import Ad
from ..plans import ST_ACTIVE, get_plan
from ..texts import limit_reached

log = logging.getLogger("dispatcher")
HOUR = 3600
MAX_ATTEMPTS = 5


class Dispatcher:
    def __init__(self, db, cfg, messenger, client, clock=time.time, per_tick=5):
        self.db, self.cfg, self.msg, self.client = db, cfg, messenger, client
        self.clock, self.per_tick = clock, per_tick

    async def capacity(self, user) -> tuple[int, int]:
        """(sent in the last rolling hour, ads still allowed right now)."""
        plan = get_plan(user["plan"])
        sent = await self.db.sent_since(user["user_id"], int(self.clock()) - HOUR)
        if not plan or user["status"] != ST_ACTIVE:
            return sent, 0
        return sent, max(0, plan.ads_per_hour - sent)

    async def dispatch_once(self) -> int:
        total = 0
        for uid in await self.db.users_with_queue():
            try:
                total += await self._serve(uid)
            except Exception:
                log.exception("dispatch for %s failed", uid)
        return total

    async def _serve(self, uid: int) -> int:
        u = await self.db.get_user(uid)
        now = int(self.clock())
        if (not u or u["status"] != ST_ACTIVE or not get_plan(u["plan"])
                or (u["expires_at"] and u["expires_at"] <= now)):
            return 0                       # not entitled: nothing leaves (jobs.py expires/cancels)
        _, cap = await self.capacity(u)
        done = 0
        for d in await self.db.queued(uid, min(cap, self.per_tick)):
            data = await self.db.get_ad(d["ad_id"])
            if not data:
                await self.db.mark_delivery(d["id"], "failed")
                continue
            ad = Ad.from_dict(data)
            if now - d["created_at"] > self.cfg.verify_after_min * 60:
                if await self.client.ad_exists(ad.url) is False:     # deleted meanwhile
                    await self.db.mark_delivery(d["id"], "gone")
                    continue
            result = await self.msg.send_ad(uid, ad)
            if result == "sent":
                await self.db.mark_delivery(d["id"], "sent")
                done += 1
                if self.cfg.send_delay:
                    await asyncio.sleep(self.cfg.send_delay)
            elif result == "blocked":
                await self.db.set_user(uid, blocked_bot=1)
                await self.db._x("UPDATE deliveries SET status='cancelled' WHERE user_id=? "
                                 "AND status='queued'", (uid,))
                return done
            else:
                await self.db.bump_attempt(d["id"])
                if d["attempts"] + 1 >= MAX_ATTEMPTS:
                    await self.db.mark_delivery(d["id"], "failed")
                break
        if done:
            await self._limit_notice(u)
        return done

    async def _limit_notice(self, u):
        sent, cap = await self.capacity(u)
        if cap > 0:
            return
        now = int(self.clock())
        if now - (u["last_limit_notice_at"] or 0) < HOUR:
            return                          # at most one notice per hour
        queued = await self.db.count_queued(u["user_id"])
        if u["plan"] == "platinum" and not queued:
            return                          # top plan: only inform when something is waiting
        await self.db.set_user(u["user_id"], last_limit_notice_at=now)
        await self.msg.user(u["user_id"], limit_reached(u["plan"], queued))
