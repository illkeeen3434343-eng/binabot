import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.bina.parser import Ad, parse_node
from app.services.dispatcher import Dispatcher
from app.services.jobs import Jobs
from app.services.messenger import Messenger
from app.texts import ad_caption
from tests.conftest import URL_A, node

BASE = 6_500_000


class FakeBot:
    def __init__(self):
        self.sent, self.photo_error, self.forbidden = [], None, set()

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=None, message="blocked")
        if self.photo_error == "all" or (self.photo_error == "url" and isinstance(photo, str)):
            raise TelegramBadRequest(method=None, message="wrong file")
        self.sent.append((chat_id, "photo-url" if isinstance(photo, str) else "photo-upload", caption))

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=None, message="blocked")
        self.sent.append((chat_id, "text", text))

    def ads(self, uid):
        return [s for s in self.sent if s[0] == uid and "‼️" in s[2]]

    def notes(self, uid):
        return [s[2] for s in self.sent if s[0] == uid and "‼️" not in s[2]]


@pytest.fixture
async def d(env):
    env.now = [time.time()]
    env.bot = FakeBot()
    env.msg = Messenger(env.bot, env.cfg, env.db, env.client)
    env.disp = Dispatcher(env.db, env.cfg, env.msg, env.client, clock=lambda: env.now[0], per_tick=100)
    env.jobs = Jobs(env.db, env.cfg, env.msg, env.mon, clock=lambda: env.now[0])

    async def feed(uid, n, start=0):
        link = (await env.db.user_links(uid))[0]["id"]
        ads = [parse_node(node(BASE + start + i)) for i in range(n)]
        await env.db.mark_seen(1, ads)
        for a in ads:
            await env.db.enqueue(uid, a.id, link)

    async def sent_at(offset):                       # move every 'sent' row into the past
        await env.db._x("UPDATE deliveries SET sent_at=sent_at-? WHERE status='sent'", (offset,))
    env.feed, env.age_sent = feed, sent_at
    return env


def test_caption_matches_the_required_layout():
    ad = parse_node(node(1, path="/items/1"))
    assert ad_caption(ad) == ("‼️ <b>Yeni elan</b>\n\n💰 Qiymət: 185 000 AZN\n🚪 Otaq sayı: 3\n"
                              "📐 Sahə: 85 m²\n🏢 Mərtəbə: 8/16\n📍 Ünvan: Nizami rayonu\n\n"
                              '🔗 <a href="https://bina.az/items/1">Elana bax</a>')
    bare = ad_caption(Ad(id="2", url="https://bina.az/items/2", location="A<b>" * 400))
    assert "Qiymət" not in bare and "&lt;b&gt;" in bare and len(bare) < 1024


@pytest.mark.parametrize("plan,limit", [("basic", 10), ("pro", 25), ("platinum", 50)])
async def test_hourly_limit_per_plan_and_queue_keeps_the_rest(d, plan, limit):
    await d.mkuser(10, plan=plan); await d.mklink(10)
    await d.feed(10, limit + 7)
    assert await d.disp.dispatch_once() == limit
    assert len(d.bot.ads(10)) == limit and await d.db.count_queued(10) == 7   # queued, not lost
    assert await d.disp.dispatch_once() == 0                                   # still capped
    note = d.bot.notes(10)
    assert len(note) == 1 and "Saatlıq limitə çatdınız" in note[0] and "Növbədə 7 elan" in note[0]
    assert ("planına keçin" in note[0]) == (plan != "platinum")               # upsell: basic/pro only


async def test_limit_is_rolling_not_calendar_hour(d):
    await d.mkuser(10, plan="basic"); await d.mklink(10)
    await d.feed(10, 4)
    await d.disp.dispatch_once()
    await d.age_sent(40 * 60)                         # those 4 were sent 40 min ago
    await d.feed(10, 12, start=100)
    assert await d.disp.dispatch_once() == 6          # 10 - 4 still inside the window
    await d.age_sent(25 * 60)                         # first 4 are now 65 min old, the 6 are 25 min old
    assert await d.disp.dispatch_once() == 4          # exactly the freed capacity
    assert await d.db.count_queued(10) == 2
    assert len(d.bot.notes(10)) == 1                  # limit notice not repeated within the hour


async def test_each_ad_is_a_separate_message_in_order_and_never_twice(d):
    await d.mkuser(10, plan="pro"); await d.mklink(10)
    await d.feed(10, 3); await d.feed(10, 3)          # second enqueue of the same ads is ignored
    await d.disp.dispatch_once(); await d.disp.dispatch_once()
    caps = [s[2] for s in d.bot.ads(10)]
    assert len(caps) == 3 and [f"/items/{BASE + i}" in c for i, c in enumerate(caps)] == [True] * 3


@pytest.mark.parametrize("status", ["pending", "rejected", "suspended", "expired"])
async def test_non_active_users_receive_nothing(d, status):
    await d.mkuser(10); await d.mklink(10)
    await d.feed(10, 3)
    await d.db.set_status(10, status)
    assert await d.disp.dispatch_once() == 0 and d.bot.sent == []
    assert await d.db.count_queued(10) == 0           # queue cancelled when access ended


async def test_image_fallbacks_never_lose_the_ad(d):
    await d.mkuser(10, plan="pro"); await d.mklink(10)
    await d.feed(10, 1)
    await d.disp.dispatch_once()
    d.bot.photo_error = "url"; await d.feed(10, 1, start=10); await d.disp.dispatch_once()
    d.bot.photo_error = "all"; await d.feed(10, 1, start=20); await d.disp.dispatch_once()
    assert [s[1] for s in d.bot.ads(10)] == ["photo-url", "photo-upload", "text"]


async def test_deleted_ad_is_dropped_after_long_wait_and_blocked_user_is_handled(d):
    await d.mkuser(10); await d.mklink(10)
    await d.feed(10, 2)
    await d.db._x("UPDATE deliveries SET created_at=created_at-7200")
    d.http.gone.add(f"https://bina.az/items/{BASE}")
    assert await d.disp.dispatch_once() == 1
    assert (await d.db.delivery_stats(0)).get("gone") == 1
    await d.mkuser(11); await d.mklink(11); await d.feed(11, 2, start=50)
    d.bot.forbidden.add(11)
    await d.disp.dispatch_once()
    assert (await d.db.get_user(11))["blocked_bot"] == 1 and await d.db.count_queued(11) == 0


async def test_expiry_and_three_daily_reminders(d):
    tz = ZoneInfo("Asia/Baku")
    start = dt.datetime(2026, 11, 14, 10, 30, tzinfo=tz).timestamp()
    d.now[0] = start
    await d.mkuser(10); await d.mklink(10)
    await d.db.set_user(10, expires_at=int(start + 2.5 * 86400))
    await d.mkuser(11); await d.db.set_user(11, expires_at=int(start + 10 * 86400))
    await d.jobs.tick(); await d.jobs.tick()                    # same day twice -> one reminder
    assert len(d.bot.notes(10)) == 1 and "3 gün" in d.bot.notes(10)[0] and d.bot.notes(11) == []
    d.now[0] = start + 86400 - 3600                             # next day 09:30: before reminder hour
    await d.jobs.tick(); assert len(d.bot.notes(10)) == 1
    d.now[0] = start + 86400; await d.jobs.tick()
    d.now[0] = start + 2 * 86400; await d.jobs.tick()
    assert ["2 gün" in d.bot.notes(10)[1], "1 gün" in d.bot.notes(10)[2]] == [True, True]
    await d.feed(10, 2)
    d.now[0] = start + 3 * 86400; await d.jobs.tick()
    u = await d.db.get_user(10)
    assert u["status"] == "expired" and "müddəti bitdi" in d.bot.notes(10)[-1]
    assert await d.disp.dispatch_once() == 0 and d.bot.ads(10) == []


async def test_queue_ttl_and_midnight_reset(d):
    tz = ZoneInfo("Asia/Baku")
    d.now[0] = dt.datetime(2026, 11, 14, 23, 50, tzinfo=tz).timestamp()
    await d.mkuser(10); await d.mklink(10)
    await d.jobs.tick()
    await d.feed(10, 3)
    await d.db._x("UPDATE deliveries SET created_at=?", (int(d.now[0]) - 60,))
    d.now[0] += 15 * 60                                          # 00:05 next day
    await d.feed(10, 1, start=30)
    await d.db._x("UPDATE deliveries SET created_at=? WHERE ad_id=?", (int(d.now[0]) - 60, str(BASE + 30)))
    await d.jobs.tick()
    assert await d.db.count_queued(10) == 1                      # yesterday's backlog dropped


async def test_daily_report_once_a_day_with_failures(d):
    tz = ZoneInfo("Asia/Baku")
    d.now[0] = dt.datetime(2026, 11, 14, 8, 0, tzinfo=tz).timestamp()
    d.cfg.admin_ids = frozenset({1})
    await d.alerts.error("Bina.az", "HTTP 429", "x"); d.bot.sent.clear()
    await d.db._x("UPDATE errors SET ts=?", (int(d.now[0]) - 100,))
    await d.jobs.tick(); assert d.bot.notes(1) == []             # before REPORT_HOUR
    d.now[0] += 3600 + 60
    await d.jobs.tick(); await d.jobs.tick()
    rep = d.bot.notes(1)
    assert len(rep) == 1 and "Gündəlik hesabat" in rep[0] and "HTTP 429] × 1" in rep[0]
