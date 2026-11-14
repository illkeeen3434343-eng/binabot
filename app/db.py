"""SQLite data layer (aiosqlite, WAL). All timestamps are UTC epoch seconds.

The only module that contains SQL. Every write is committed immediately, so a
power cut or VM restart loses at most the operation in flight.
"""
from __future__ import annotations

import json
import os
import time

import aiosqlite

from .plans import PLANS, ST_ACTIVE, ST_EXPIRED, ST_NEW, ST_PENDING

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    registered_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    plan TEXT,
    requested_plan TEXT, requested_at INTEGER,
    activated_at INTEGER, expires_at INTEGER,
    state TEXT,
    last_reminder_day TEXT,
    last_limit_notice_at INTEGER NOT NULL DEFAULT 0,
    blocked_bot INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    filter_json TEXT NOT NULL,
    client_filter_json TEXT NOT NULL DEFAULT '{}',
    seeded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_checked_at INTEGER, last_ok_at INTEGER, last_error TEXT
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    search_id INTEGER NOT NULL REFERENCES searches(id),
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, search_id)
);
CREATE TABLE IF NOT EXISTS ads (
    ad_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    first_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS search_seen (
    search_id INTEGER NOT NULL, ad_id TEXT NOT NULL, seen_at INTEGER NOT NULL,
    PRIMARY KEY (search_id, ad_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, ad_id TEXT NOT NULL, link_id INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|sent|expired|gone|cancelled|failed
    created_at INTEGER NOT NULL, sent_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, ad_id)
);
CREATE INDEX IF NOT EXISTS ix_deliv_user ON deliveries(user_id, status, sent_at);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL, finished_at INTEGER, trigger TEXT,
    searches_ok INTEGER DEFAULT 0, searches_failed INTEGER DEFAULT 0,
    new_ads INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, source TEXT NOT NULL, code TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
    target_user INTEGER, details TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL, action TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, action, day)
) WITHOUT ROWID;
"""


def now_ts() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str):
        self.path = path
        self.c: aiosqlite.Connection | None = None

    async def open(self) -> "Database":
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.c = await aiosqlite.connect(self.path)
        self.c.row_factory = aiosqlite.Row
        await self.c.execute("PRAGMA journal_mode=WAL")
        await self.c.execute("PRAGMA synchronous=NORMAL")
        await self.c.execute("PRAGMA foreign_keys=ON")
        await self.c.executescript(SCHEMA)
        cols = {r[1] for r in await self._all("PRAGMA table_info(users)")}
        if "note" not in cols:                      # migration for databases created before notes
            await self.c.execute("ALTER TABLE users ADD COLUMN note TEXT")
        await self.c.commit()
        return self

    async def close(self):
        if self.c:
            await self.c.close()
            self.c = None

    async def _x(self, sql, args=()):
        cur = await self.c.execute(sql, args)
        await self.c.commit()
        return cur

    async def _one(self, sql, args=()):
        cur = await self.c.execute(sql, args)
        return await cur.fetchone()

    async def _all(self, sql, args=()):
        cur = await self.c.execute(sql, args)
        return await cur.fetchall()

    async def _val(self, sql, args=()):
        r = await self._one(sql, args)
        return r[0] if r else None

    # ------------------------------------------------------------- kv
    async def kv_get(self, key, default=None):
        v = await self._val("SELECT value FROM kv WHERE key=?", (key,))
        return default if v is None else v

    async def kv_set(self, key, value):
        await self._x("INSERT INTO kv(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ------------------------------------------------------------- users
    async def upsert_user(self, user_id, username, first_name):
        """Register on first contact; refresh the name afterwards. Returns (row, created)."""
        cur = await self._x(
            "INSERT OR IGNORE INTO users(user_id, username, first_name, registered_at) "
            "VALUES(?,?,?,?)", (user_id, username, first_name, now_ts()))
        created = cur.rowcount == 1
        if not created:
            await self._x("UPDATE users SET username=?, first_name=?, blocked_bot=0 WHERE user_id=?",
                          (username, first_name, user_id))
        return await self.get_user(user_id), created

    async def get_user(self, user_id):
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def set_state(self, user_id, state):
        await self._x("UPDATE users SET state=? WHERE user_id=?", (state, user_id))

    async def set_user(self, user_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._x(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    async def set_status(self, user_id, status):
        """Change status; leaving 'active' cancels whatever is still queued."""
        await self.set_user(user_id, status=status)
        if status != ST_ACTIVE:
            await self._x("UPDATE deliveries SET status='cancelled' "
                          "WHERE user_id=? AND status='queued'", (user_id,))

    async def request_plan(self, user_id, plan):
        u = await self.get_user(user_id)
        fields = {"requested_plan": plan, "requested_at": now_ts()}
        if u["status"] != ST_ACTIVE:
            fields["status"] = ST_PENDING
        await self.set_user(user_id, **fields)

    async def activate(self, user_id, plan, expires_at):
        await self.set_user(user_id, status=ST_ACTIVE, plan=plan, expires_at=expires_at,
                            activated_at=now_ts(), requested_plan=None, requested_at=None,
                            last_reminder_day=None)

    async def list_users(self, offset=0, limit=8):
        return await self._all("SELECT * FROM users ORDER BY registered_at DESC, user_id DESC "
                               "LIMIT ? OFFSET ?", (limit, offset))

    async def count_users(self, status=None):
        if status:
            return await self._val("SELECT COUNT(*) FROM users WHERE status=?", (status,))
        return await self._val("SELECT COUNT(*) FROM users")

    async def status_counts(self):
        return {r[0]: r[1] for r in await self._all("SELECT status, COUNT(*) FROM users GROUP BY status")}

    async def pending_requests(self):
        return await self._all("SELECT * FROM users WHERE requested_plan IS NOT NULL "
                               "ORDER BY requested_at")

    async def due_expired(self, ts):
        return await self._all("SELECT * FROM users WHERE status=? AND expires_at IS NOT NULL "
                               "AND expires_at<=?", (ST_ACTIVE, ts))

    async def expiring_between(self, lo, hi):
        return await self._all("SELECT * FROM users WHERE status=? AND expires_at>? AND expires_at<=?",
                               (ST_ACTIVE, lo, hi))

    # ------------------------------------------------------------- searches / links
    async def get_or_create_search(self, parsed):
        await self._x("INSERT OR IGNORE INTO searches(url, filter_json, client_filter_json, created_at) "
                      "VALUES(?,?,?,?)", (parsed.url, json.dumps(parsed.filter),
                                          json.dumps(parsed.client_filter), now_ts()))
        return await self._val("SELECT id FROM searches WHERE url=?", (parsed.url,))

    async def user_links(self, user_id):
        return await self._all(
            "SELECT l.id, l.user_id, l.search_id, l.created_at, s.url, s.last_ok_at, s.last_error "
            "FROM links l JOIN searches s ON s.id=l.search_id WHERE l.user_id=? ORDER BY l.id",
            (user_id,))

    async def count_links(self, user_id):
        return await self._val("SELECT COUNT(*) FROM links WHERE user_id=?", (user_id,))

    async def get_link(self, link_id, user_id=None):
        """With user_id the link is returned only to its owner."""
        sql = ("SELECT l.id, l.user_id, l.search_id, l.created_at, s.url FROM links l "
               "JOIN searches s ON s.id=l.search_id WHERE l.id=?")
        args = [link_id]
        if user_id is not None:
            sql += " AND l.user_id=?"
            args.append(user_id)
        return await self._one(sql, args)

    async def add_link(self, user_id, parsed):
        """Returns link id, or None when this user already tracks this search."""
        sid = await self.get_or_create_search(parsed)
        cur = await self._x("INSERT OR IGNORE INTO links(user_id, search_id, created_at) VALUES(?,?,?)",
                            (user_id, sid, now_ts()))
        return cur.lastrowid if cur.rowcount == 1 else None

    async def _cancel_link_queue(self, link_id):
        await self._x("UPDATE deliveries SET status='cancelled' WHERE link_id=? AND status='queued'",
                      (link_id,))

    async def update_link(self, link_id, user_id, parsed):
        """Point an existing link at another search. False = duplicate or not the owner."""
        if not await self.get_link(link_id, user_id):
            return False
        sid = await self.get_or_create_search(parsed)
        if await self._val("SELECT 1 FROM links WHERE user_id=? AND search_id=?", (user_id, sid)):
            return False
        await self._x("UPDATE links SET search_id=?, created_at=? WHERE id=? AND user_id=?",
                      (sid, now_ts(), link_id, user_id))
        await self._cancel_link_queue(link_id)
        return True

    async def delete_link(self, link_id, user_id=None):
        if not await self.get_link(link_id, user_id):
            return False
        await self._cancel_link_queue(link_id)
        await self._x("DELETE FROM links WHERE id=?", (link_id,))
        return True

    async def monitored(self):
        """{search_row: [link rows]} for links that are entitled to monitoring now:
        active, unexpired user and within the plan's link allowance (oldest links win,
        so a downgrade pauses the extra links instead of deleting them)."""
        rows = await self._all(
            "SELECT l.id AS link_id, l.user_id, l.search_id, u.plan FROM links l "
            "JOIN users u ON u.user_id=l.user_id "
            "WHERE u.status=? AND (u.expires_at IS NULL OR u.expires_at>?) ORDER BY l.id",
            (ST_ACTIVE, now_ts()))
        per_user: dict[int, int] = {}
        by_search: dict[int, list] = {}
        for r in rows:
            plan = PLANS.get(r["plan"] or "")
            if not plan:
                continue
            n = per_user.get(r["user_id"], 0)
            if n >= plan.max_links:
                continue
            per_user[r["user_id"]] = n + 1
            by_search.setdefault(r["search_id"], []).append(r)
        if not by_search:
            return []
        q = ",".join("?" * len(by_search))
        searches = await self._all(f"SELECT * FROM searches WHERE id IN ({q}) ORDER BY id",
                                   tuple(by_search))
        return [(s, by_search[s["id"]]) for s in searches]

    async def search_result(self, search_id, ok, error=None, seeded=None):
        t = now_ts()
        if ok:
            await self._x("UPDATE searches SET last_checked_at=?, last_ok_at=?, last_error=NULL, "
                          "seeded=COALESCE(?, seeded) WHERE id=?", (t, t, seeded, search_id))
        else:
            await self._x("UPDATE searches SET last_checked_at=?, last_error=? WHERE id=?",
                          (t, error, search_id))

    # ------------------------------------------------------------- ads / history
    async def seen_ids(self, search_id, ad_ids):
        if not ad_ids:
            return set()
        q = ",".join("?" * len(ad_ids))
        rows = await self._all(f"SELECT ad_id FROM search_seen WHERE search_id=? AND ad_id IN ({q})",
                               (search_id, *ad_ids))
        return {r[0] for r in rows}

    async def mark_seen(self, search_id, ads):
        t = now_ts()
        await self.c.executemany("INSERT OR IGNORE INTO search_seen(search_id, ad_id, seen_at) VALUES(?,?,?)",
                                 [(search_id, a.id, t) for a in ads])
        await self.c.executemany("INSERT OR IGNORE INTO ads(ad_id, data, first_seen) VALUES(?,?,?)",
                                 [(a.id, json.dumps(a.to_dict(), ensure_ascii=False), t) for a in ads])
        await self.c.commit()

    async def get_ad(self, ad_id):
        v = await self._val("SELECT data FROM ads WHERE ad_id=?", (ad_id,))
        return json.loads(v) if v else None

    # ------------------------------------------------------------- deliveries
    async def enqueue(self, user_id, ad_id, link_id):
        """Queue an ad for a user once, ever (UNIQUE user+ad survives restarts)."""
        cur = await self._x("INSERT OR IGNORE INTO deliveries(user_id, ad_id, link_id, created_at) "
                            "VALUES(?,?,?,?)", (user_id, ad_id, link_id, now_ts()))
        return cur.rowcount == 1

    async def users_with_queue(self):
        rows = await self._all("SELECT DISTINCT user_id FROM deliveries WHERE status='queued'")
        return [r[0] for r in rows]

    async def queued(self, user_id, limit):
        return await self._all("SELECT * FROM deliveries WHERE user_id=? AND status='queued' "
                               "ORDER BY id LIMIT ?", (user_id, limit))

    async def count_queued(self, user_id=None):
        if user_id is None:
            return await self._val("SELECT COUNT(*) FROM deliveries WHERE status='queued'")
        return await self._val("SELECT COUNT(*) FROM deliveries WHERE user_id=? AND status='queued'",
                               (user_id,))

    async def sent_since(self, user_id, ts):
        return await self._val("SELECT COUNT(*) FROM deliveries WHERE user_id=? AND status='sent' "
                               "AND sent_at>?", (user_id, ts))

    async def oldest_sent_since(self, user_id, ts):
        return await self._val("SELECT MIN(sent_at) FROM deliveries WHERE user_id=? AND status='sent' "
                               "AND sent_at>?", (user_id, ts))

    async def mark_delivery(self, delivery_id, status):
        sent = now_ts() if status == "sent" else None
        await self._x("UPDATE deliveries SET status=?, sent_at=?, attempts=attempts+1 WHERE id=?",
                      (status, sent, delivery_id))

    async def bump_attempt(self, delivery_id):
        await self._x("UPDATE deliveries SET attempts=attempts+1 WHERE id=?", (delivery_id,))

    async def expire_queue(self, before_ts):
        cur = await self._x("UPDATE deliveries SET status='expired' WHERE status='queued' "
                            "AND created_at<?", (before_ts,))
        return cur.rowcount

    async def delivery_stats(self, since):
        rows = await self._all("SELECT status, COUNT(*) FROM deliveries WHERE "
                               "COALESCE(sent_at, created_at)>=? GROUP BY status", (since,))
        return {r[0]: r[1] for r in rows}

    async def top_receivers(self, since, limit=5):
        return await self._all("SELECT user_id, COUNT(*) n FROM deliveries WHERE status='sent' "
                               "AND sent_at>=? GROUP BY user_id ORDER BY n DESC LIMIT ?", (since, limit))

    # ------------------------------------------------------------- cycles / errors / audit
    async def cycle_start(self, trigger):
        cur = await self._x("INSERT INTO cycles(started_at, trigger) VALUES(?,?)", (now_ts(), trigger))
        return cur.lastrowid

    async def cycle_finish(self, cid, ok, failed, new_ads, note=None):
        await self._x("UPDATE cycles SET finished_at=?, searches_ok=?, searches_failed=?, new_ads=?, "
                      "note=? WHERE id=?", (now_ts(), ok, failed, new_ads, note, cid))

    async def last_cycle(self):
        return await self._one("SELECT * FROM cycles ORDER BY id DESC LIMIT 1")

    async def cycle_stats(self, since):
        return await self._one(
            "SELECT COUNT(*) n, COALESCE(SUM(searches_ok),0) ok, COALESCE(SUM(searches_failed),0) failed, "
            "COALESCE(SUM(new_ads),0) new_ads, "
            "SUM(CASE WHEN finished_at IS NULL OR note IS NOT NULL THEN 1 ELSE 0 END) bad "
            "FROM cycles WHERE started_at>=?", (since,))

    async def add_error(self, source, code, message):
        await self._x("INSERT INTO errors(ts, source, code, message) VALUES(?,?,?,?)",
                      (now_ts(), source, str(code), str(message)[:500]))

    async def recent_errors(self, limit=10):
        return await self._all("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))

    async def error_summary(self, since):
        return await self._all("SELECT source, code, COUNT(*) n, MAX(ts) last FROM errors WHERE ts>=? "
                               "GROUP BY source, code ORDER BY n DESC", (since,))

    async def add_audit(self, admin_id, action, target_user=None, details=""):
        await self._x("INSERT INTO audit(ts, admin_id, action, target_user, details) VALUES(?,?,?,?,?)",
                      (now_ts(), admin_id, action, target_user, details))

    async def recent_audit(self, limit=10):
        return await self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- usage statistics
    async def track(self, user_id, action):
        if not action:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        await self._x("INSERT INTO usage(user_id, action, day, n) VALUES(?,?,?,1) "
                      "ON CONFLICT(user_id, action, day) DO UPDATE SET n=n+1", (user_id, action, day))

    async def usage_summary(self, days=7, top=8):
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return {
            "active_today": await self._val("SELECT COUNT(DISTINCT user_id) FROM usage WHERE day=?", (today,)),
            "active_period": await self._val("SELECT COUNT(DISTINCT user_id) FROM usage WHERE day>=?", (since,)),
            "actions": await self._all("SELECT action, SUM(n) n FROM usage WHERE day>=? AND action NOT LIKE 'a:%' "
                                       "GROUP BY action ORDER BY n DESC LIMIT ?", (since, top)),
            "users": await self._all("SELECT user_id, SUM(n) n FROM usage WHERE day>=? AND action NOT LIKE 'a:%' "
                                     "GROUP BY user_id ORDER BY n DESC LIMIT ?", (since, top)),
        }

    async def active_user_ids(self):
        rows = await self._all("SELECT user_id FROM users WHERE status=? AND blocked_bot=0", (ST_ACTIVE,))
        return [r[0] for r in rows]

    # ------------------------------------------------------------- housekeeping
    async def prune(self, keep_days=60):
        cut = now_ts() - keep_days * 86400
        await self._x("DELETE FROM searches WHERE id NOT IN (SELECT search_id FROM links) AND created_at<?",
                      (now_ts() - 7 * 86400,))
        await self._x("DELETE FROM search_seen WHERE seen_at<? OR search_id NOT IN (SELECT id FROM searches)",
                      (cut,))
        await self._x("DELETE FROM ads WHERE first_seen<? AND ad_id NOT IN "
                      "(SELECT ad_id FROM deliveries WHERE status='queued')", (cut,))
        await self._x("DELETE FROM errors WHERE ts<?", (cut,))
        await self._x("DELETE FROM cycles WHERE started_at<?", (cut,))
        await self._x("DELETE FROM usage WHERE day<?", (time.strftime("%Y-%m-%d", time.gmtime(cut)),))
