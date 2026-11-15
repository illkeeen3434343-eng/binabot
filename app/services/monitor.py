"""Monitoring engine: one fetch per distinct search, fan-out to every entitled user.

New-ad rule (per search):
  * identity = Bina.az listing ID; every ID ever seen for a search is stored (search_seen)
  * the first successful read of a search is a silent baseline
  * an unseen ID is announced only if the ad is actually recent. Bina.az sorts by
    "bumped at", so a paid bump lifts OLD ads to the top; their age is known from the
    creation date or, failing that, from the ID itself (IDs grow ~2800/day site-wide,
    measured in the existing project). Old + unseen -> remembered, not announced.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..bina.client import BinaBlocked, BinaUnavailable
from ..bina.parser import SchemaError

log = logging.getLogger("monitor")


class Monitor:
    def __init__(self, db, cfg, client, alerts, clock=time.time):
        self.db, self.cfg, self.client, self.alerts = db, cfg, client, alerts
        self.clock = clock
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- cool-down
    async def blocked_until(self) -> float:
        return float(await self.db.kv_get("bina_blocked_until", 0))

    async def _block(self, exc: BinaBlocked):
        streak = int(await self.db.kv_get("bina_block_streak", 0)) + 1
        minutes = min(self.cfg.block_cooldown_max_min, self.cfg.block_cooldown_min * 2 ** (streak - 1))
        pause = max(minutes * 60, exc.retry_after or 0)
        await self.db.kv_set("bina_block_streak", streak)
        await self.db.kv_set("bina_blocked_until", self.clock() + pause)
        await self.db.kv_set("bina_unhealthy", 1)
        await self.alerts.error("Bina.az", f"HTTP {exc.status}",
                                f"Bina.az sorğunu rədd etdi (HTTP {exc.status}). "
                                f"Sorğular {int(pause // 60)} dəqiqəlik dayandırıldı (ardıcıl blok: {streak}).")

    # ----------------------------------------------------------- freshness
    def _is_fresh(self, ad, max_id: int) -> bool:
        limit = self.cfg.new_max_age_days
        if ad.created_ts:
            return (self.clock() - ad.created_ts) / 86400 <= limit
        if ad.id.isdigit() and max_id and self.cfg.ids_per_day > 0:
            return (max_id - int(ad.id)) / self.cfg.ids_per_day <= limit
        return True                       # age unknown -> do not hide a possibly new ad

    # ----------------------------------------------------------- one search
    async def _process(self, search, links, max_id_box) -> int:
        sid = search["id"]
        flt = json.loads(search["filter_json"])
        cflt = json.loads(search["client_filter_json"] or "{}")
        stale = (search["last_ok_at"] is None or
                 self.clock() - search["last_ok_at"] > self.cfg.rebaseline_hours * 3600)
        baseline = not search["seeded"] or stale

        async def keep_going(page_ads):
            seen = await self.db.seen_ids(sid, [a.id for a in page_ads])
            return any(a.id not in seen for a in page_ads)

        if baseline:
            ads = await self.client.search(flt, self.cfg.seed_pages)
        else:
            ads = await self.client.search(flt, self.cfg.max_pages, keep_going)

        for a in ads:
            if a.id.isdigit():
                max_id_box[0] = max(max_id_box[0], int(a.id))
        seen = await self.db.seen_ids(sid, [a.id for a in ads])
        unseen = list({a.id: a for a in ads if a.id not in seen}.values())
        await self.db.mark_seen(sid, unseen)
        queued = 0
        if not baseline:
            for ad in sorted(unseen, key=lambda a: int(a.id) if a.id.isdigit() else 0):
                if any(getattr(ad, k, None) is not v for k, v in cflt.items()):
                    continue                              # fails a locally applied filter
                if not self._is_fresh(ad, max_id_box[0]):
                    log.info("search %s: ad %s unseen but old (bump/promotion) - not announced", sid, ad.id)
                    continue
                for ln in links:
                    if await self.db.enqueue(ln["user_id"], ad.id, ln["link_id"]):
                        queued += 1
        await self.db.search_result(sid, True, seeded=1)
        return queued

    # ----------------------------------------------------------- cycle
    async def run_cycle(self, trigger: str = "auto", force: bool = False) -> dict:
        if self._lock.locked():
            return {"skipped": "running"}
        async with self._lock:
            until = await self.blocked_until()
            if until > self.clock() and not force:
                return {"skipped": "cooldown", "until": until}
            todo = await self.db.monitored()
            cid = await self.db.cycle_start(trigger)
            res = {"searches": len(todo), "ok": 0, "failed": 0, "queued": 0, "note": None}
            max_id_box = [int(await self.db.kv_get("bina_max_id", 0))]
            abort = asyncio.Event()
            sem = asyncio.Semaphore(self.cfg.concurrency)

            async def one(search, links):
                async with sem:
                    if abort.is_set():
                        return
                    try:
                        res["queued"] += await self._process(search, links, max_id_box)
                        res["ok"] += 1
                    except BinaBlocked as e:
                        abort.set()                       # stop knocking immediately
                        res["failed"] += 1
                        res["note"] = f"HTTP {e.status}"
                        await self.db.search_result(search["id"], False, f"HTTP {e.status}")
                        await self._block(e)
                    except BinaUnavailable as e:
                        abort.set()
                        res["failed"] += 1
                        res["note"] = str(e)
                        await self.db.search_result(search["id"], False, str(e))
                        await self.db.kv_set("bina_unhealthy", 1)
                        await self.alerts.error("Bina.az", "network", f"Bina.az-a qoşulmaq olmadı: {e}")
                    except SchemaError as e:
                        res["failed"] += 1
                        res["note"] = "struktur"
                        await self.db.search_result(search["id"], False, str(e))
                        await self.db.kv_set("bina_unhealthy", 1)
                        await self.alerts.error("Bina.az", "schema",
                                                f"Cavabın strukturu gözlənilən deyil ({search['url']}): {e}")
                    except Exception as e:                # a bug must not kill the loop
                        res["failed"] += 1
                        res["note"] = e.__class__.__name__
                        log.exception("search %s failed", search["id"])
                        await self.alerts.error("monitor", e.__class__.__name__, str(e))

            await asyncio.gather(*(one(s, l) for s, l in todo))
            await self.db.kv_set("bina_max_id", max_id_box[0])
            if res["ok"] and not res["failed"]:
                await self.db.kv_set("bina_block_streak", 0)
                if await self.db.kv_get("bina_unhealthy", "0") == "1":
                    await self.db.kv_set("bina_unhealthy", 0)
                    await self.alerts.recovered("Bina.az")
            await self.db.cycle_finish(cid, res["ok"], res["failed"], res["queued"], res["note"])
            log.info("cycle %s (%s): %s", cid, trigger, res)
            return res
