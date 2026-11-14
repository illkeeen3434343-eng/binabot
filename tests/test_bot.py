"""Drives the real aiogram dispatcher + handlers with fabricated Telegram updates.
Only the HTTP transport to Telegram is replaced."""
import datetime as dt
import time

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import Chat, Message, Update

from app import texts as T
from app.bot.common import Ctx
from app.main import build_dispatcher
from app.services.dispatcher import Dispatcher
from app.services.jobs import Jobs
from app.services.messenger import Messenger
from tests.conftest import URL_A, URL_B, node

ADMIN, U1, U2 = 1, 100, 200
BASE = 6_500_000


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.out, self.alerts, self._id = [], [], 1000

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self._id += 1
        if isinstance(method, AnswerCallbackQuery):
            self.alerts.append(method.text)
            return True
        if isinstance(method, (SendMessage, EditMessageText, SendPhoto)):
            text = getattr(method, "text", None) or getattr(method, "caption", None)
            kind = "photo" if isinstance(method, SendPhoto) else "text"
            self.out.append((int(method.chat_id), text, method.reply_markup, kind))
            return Message(message_id=self._id, date=dt.datetime.now(), chat=Chat(id=int(method.chat_id), type="private"), text=text)
        return True


class Tg:
    def __init__(self, dp, bot, session):
        self.dp, self.bot, self.s, self.n = dp, bot, session, 0

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"Ad{uid}", "username": f"user{uid}"}

    async def text(self, uid, text):
        self.n += 1
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.n, "message": {
            "message_id": self.n, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": self._user(uid), "text": text,
            **({"entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
               if text.startswith("/") else {})}}))
        return self.last(uid)

    async def click(self, uid, data):
        assert data in self.buttons(uid), f"button {data!r} is not on {uid}'s screen: {self.buttons(uid)}"
        return await self.press(uid, data)

    async def press(self, uid, data):             # forged callback, button need not be visible
        self.n += 1
        self.s.alerts.clear()
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.n, "callback_query": {
            "id": str(self.n), "from": self._user(uid), "chat_instance": "x", "data": data,
            "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": {"id": 999, "is_bot": True, "first_name": "bot"}, "text": "old"}}}))
        return self.last(uid)

    def last(self, uid):
        return next((o[1] for o in reversed(self.s.out) if o[0] == uid), "")

    def buttons(self, uid):
        mk = next((o[2] for o in reversed(self.s.out) if o[0] == uid and o[2]), None)
        return [b.callback_data for row in mk.inline_keyboard for b in row] if mk else []

    def labels(self, uid):
        mk = next((o[2] for o in reversed(self.s.out) if o[0] == uid and o[2]), None)
        return [b.text for row in mk.inline_keyboard for b in row] if mk else []

    def all(self, uid):
        return [o[1] for o in self.s.out if o[0] == uid]


@pytest.fixture
async def tg(env):
    session = FakeSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    env.msg = Messenger(bot, env.cfg, env.db, env.client)
    env.alerts._send = env.msg.admins
    env.disp = Dispatcher(env.db, env.cfg, env.msg, env.client, per_tick=100)
    env.jobs = Jobs(env.db, env.cfg, env.msg, env.mon)
    env.ctx = Ctx(cfg=env.cfg, db=env.db, lmap=env.lmap, monitor=env.mon, dispatcher=env.disp, msg=env.msg)
    from app.bot import admin, user
    admin.router._parent_router = user.router._parent_router = None   # test-only: fresh dispatcher per test
    t = Tg(build_dispatcher(env.ctx, env.alerts), bot, session)
    t.env = env
    return t


async def approve(tg, uid, plan="basic", days="30"):
    await tg.text(uid, "/start"); await tg.click(uid, "m:subs")
    await tg.click(uid, f"s:p:{plan}"); await tg.click(uid, f"s:c:{plan}")
    await tg.click(ADMIN, f"a:ap:{uid}"); await tg.click(ADMIN, f"a:ad:{uid}:{days}")


async def add_link(tg, uid, url):
    await tg.click(uid, "m:home"); await tg.click(uid, "m:links"); await tg.click(uid, "l:add")
    return await tg.text(uid, url)


async def test_full_scenario_start_to_notifications(tg):
    env, db = tg.env, tg.env.db
    # --- start: registration + welcome + main menu with the requested buttons
    await tg.text(U1, "/start")
    u = await db.get_user(U1)
    assert (u["username"], u["first_name"], u["status"]) == ("user100", "Ad100", "new") and u["registered_at"]
    assert "Necə başlamaq olar" in tg.all(U1)[0]
    assert tg.labels(U1) == [T.BTN_STATUS, T.BTN_LINKS, T.BTN_SUBS, T.BTN_HELP]      # no admin button
    # --- cannot add links before activation
    await tg.click(U1, "m:links")
    assert "aktiv abunəliyiniz olmalıdır" in await tg.click(U1, "l:add")
    # --- subscribe -> pending -> admin notified with Approve / Reject
    await tg.click(U1, "m:subs"); await tg.click(U1, "s:p:pro")
    assert "sorğunuz adminə göndərildi" in await tg.click(U1, "s:c:pro") and "Kart: 0000" in tg.last(U1)
    assert (await db.get_user(U1))["status"] == "pending"
    assert "Yeni abunəlik sorğusu" in tg.last(ADMIN) and tg.buttons(ADMIN)[:2] == [f"a:ap:{U1}", f"a:rj:{U1}"]
    await tg.click(U1, "m:home")
    assert "Gözləmədə" in await tg.click(U1, "m:status")
    # --- admin approves with an expiry date -> active
    await tg.click(ADMIN, f"a:ap:{U1}")
    await tg.click(ADMIN, f"a:ad:{U1}:30")
    u = await db.get_user(U1)
    assert u["status"] == "active" and u["plan"] == "pro" and 29.9 < (u["expires_at"] - time.time()) / 86400 <= 30
    assert "Abunəliyiniz təsdiqləndi" in tg.last(U1)
    # --- links: invalid / deceptive / duplicate / limit
    assert await add_link(tg, U1, "https://bina.az.example.com/baki/alqi-satqi/menziller") == T.INVALID_LINK
    assert await tg.text(U1, "https://tap.az/elanlar") == T.INVALID_LINK
    assert "dəstəklənmir" in await tg.text(U1, "https://bina.az/items/5")
    assert await db.count_links(U1) == 0
    assert "Link əlavə olundu" in await tg.text(U1, URL_A)
    assert await add_link(tg, U1, "http://www.bina.az/baki/alqi-satqi/menziller/?room_ids%5B%5D=3&page=2") == T.DUPLICATE_LINK
    assert "Link əlavə olundu" in await tg.text(U1, URL_B)
    assert "Link əlavə olundu" in await add_link(tg, U1, URL_A + "&price_to=1")
    assert "maksimum link sayına" in await tg.click(U1, "l:add")                      # pro = 3
    # --- status card
    await tg.click(U1, "m:home")
    card = await tg.click(U1, "m:status")
    for part in (f"<code>{U1}</code>", "✅ Aktiv", "Plan: Pro", "Linklər: 3 / 3", "göndərilən: 0 / 25",
                 "Qalan saatlıq limit: 25", "Aktivləşmə tarixi: ", "Bitmə tarixi: "):
        assert part in card, part
    # --- monitoring: baseline silent, then a new ad arrives as photo with the caption
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]
    await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2), node(BASE + 1)]]
    await env.mon.run_cycle()
    assert await env.disp.dispatch_once() == 1                     # 2 links match, 1 message
    chat, cap, _, kind = tg.s.out[-1]
    assert (chat, kind) == (U1, "photo") and cap.startswith("‼️ <b>Yeni elan</b>") and "185 000 AZN" in cap


async def test_navigation_back_and_home(tg):
    await approve(tg, U1); await add_link(tg, U1, URL_A)
    lid = (await tg.env.db.user_links(U1))[0]["id"]
    await tg.click(U1, f"l:v:{lid}")
    assert tg.buttons(U1)[-2:] == ["m:links", "m:home"]            # Back -> links list, Home -> main
    await tg.click(U1, f"l:d:{lid}")
    assert tg.buttons(U1)[-2:] == [f"l:v:{lid}", "m:home"]         # Back -> the link card
    assert "Link 1" in await tg.click(U1, f"l:v:{lid}")
    assert "Linklərim" in await tg.click(U1, "m:links")
    assert "Ana səhifə" in await tg.click(U1, "m:home")
    for screen in ("m:status", "m:subs", "m:help"):
        await tg.click(U1, screen)
        assert tg.buttons(U1)[-2:] == ["m:home", "m:home"] and tg.labels(U1)[-2:] == [T.BTN_BACK, T.BTN_HOME]
        await tg.click(U1, "m:home")


async def test_edit_and_delete_need_confirmation_and_cancel_keeps_link(tg):
    db = tg.env.db
    await approve(tg, U1, "pro"); await add_link(tg, U1, URL_A)
    lid = (await db.user_links(U1))[0]["id"]
    await tg.click(U1, f"l:v:{lid}")
    assert "silmək istədiyinizə əminsiniz" in await tg.click(U1, f"l:d:{lid}")
    await tg.click(U1, f"l:v:{lid}")                                # "Xeyr"
    assert await db.count_links(U1) == 1
    assert "dəyişmək istədiyinizə əminsiniz" in await tg.click(U1, f"l:e:{lid}")
    assert await tg.click(U1, f"l:ey:{lid}") == T.ASK_NEW_LINK
    assert await tg.text(U1, "not a link") == T.INVALID_LINK
    assert "Link yeniləndi" in await tg.text(U1, URL_B)
    assert (await db.user_links(U1))[0]["url"].startswith("https://bina.az/baki/kiraye")
    await tg.click(U1, f"l:v:{lid}"); await tg.click(U1, f"l:d:{lid}")
    assert "Link silindi" in await tg.click(U1, f"l:dy:{lid}")
    assert await db.count_links(U1) == 0
    # pressing a button abandons a pending text dialogue
    await tg.click(U1, "l:add"); await tg.click(U1, "m:links")
    assert await tg.text(U1, URL_A) == T.UNKNOWN_INPUT and await db.count_links(U1) == 0


async def test_user_cannot_touch_another_users_link(tg):
    db = tg.env.db
    await approve(tg, U1); await add_link(tg, U1, URL_A)
    await approve(tg, U2); await add_link(tg, U2, URL_B)
    victim = (await db.user_links(U1))[0]["id"]
    for data in (f"l:v:{victim}", f"l:e:{victim}", f"l:ey:{victim}", f"l:d:{victim}", f"l:dy:{victim}"):
        assert await tg.press(U2, data) == T.LINK_NOT_FOUND
    await db.set_state(U2, f"edit_link:{victim}")                   # forged state
    assert await tg.text(U2, URL_A + "&price_to=5") == T.LINK_NOT_FOUND
    assert (await db.user_links(U1))[0]["url"].endswith("room_ids%5B%5D=3")


async def test_admin_area_is_closed_to_non_admins(tg):
    db = tg.env.db
    await approve(tg, U1)
    await tg.text(U2, "/start")
    for data in ("a:home", f"a:ap:{U1}", f"a:ad:{U2}:30", f"a:su:{U1}", f"a:ps:{U1}:platinum", "a:run", "a:us:0"):
        n = len(tg.s.out)
        await tg.press(U2, data)
        assert tg.s.alerts == [T.NO_ACCESS] and len(tg.s.out) == n
    assert await tg.text(U2, "/admin") == T.UNKNOWN_INPUT
    await db.set_state(U2, f"adm_date:adc:{U2}")
    await tg.text(U2, "31.12.2030")
    assert (await db.get_user(U2))["status"] == "new" and (await db.get_user(U1))["status"] == "active"
    assert await db.recent_audit() and all(r["admin_id"] == ADMIN for r in await db.recent_audit())


async def test_admin_user_management_and_audit(tg):
    env, db = tg.env, tg.env.db
    await approve(tg, U1, "basic"); await add_link(tg, U1, URL_A)
    await tg.text(U2, "/start"); await tg.click(U2, "m:subs"); await tg.click(U2, "s:p:basic"); await tg.click(U2, "s:c:basic")
    # panel, pending list, user list
    assert "Admin panel" in await tg.text(ADMIN, "/admin") and "⏳ Sorğular (1)" in tg.labels(ADMIN)
    await tg.click(ADMIN, "a:pend"); assert f"a:u:{U2}" in tg.buttons(ADMIN)
    await tg.click(ADMIN, "a:home"); await tg.click(ADMIN, "a:us:0")
    assert {f"a:u:{U1}", f"a:u:{U2}", f"a:u:{ADMIN}"} <= set(tg.buttons(ADMIN))
    # reject
    await tg.click(ADMIN, f"a:u:{U2}"); await tg.click(ADMIN, f"a:rj:{U2}")
    assert (await db.get_user(U2))["status"] == "rejected" and tg.last(U2) == T.REJECTED
    # change plan: limits follow automatically
    await tg.click(ADMIN, "a:us:0"); await tg.click(ADMIN, f"a:u:{U1}"); await tg.click(ADMIN, f"a:pl:{U1}")
    await tg.click(ADMIN, f"a:ps:{U1}:platinum")
    assert "Platinum" in tg.last(U1)
    await tg.click(U1, "m:home"); card = await tg.click(U1, "m:status")
    assert "Linklər: 1 / 5" in card and "0 / 50" in card
    # custom expiry date typed by the admin
    await tg.click(ADMIN, f"a:ex:{U1}"); await tg.click(ADMIN, f"a:esc:{U1}")
    assert await tg.text(ADMIN, "01.01.2020") and "düzgün deyil" in tg.all(ADMIN)[-1]
    await tg.text(ADMIN, "31.12.2030")
    assert T.fmt_dt((await db.get_user(U1))["expires_at"], "Asia/Baku") == "31.12.2030 23:59"
    assert "31.12.2030" in tg.last(U1)
    # suspend -> nothing is delivered; reactivate -> delivery resumes
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]; await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 2), node(BASE + 1)]]; await env.mon.run_cycle()
    await tg.click(ADMIN, f"a:su:{U1}")
    assert (await db.get_user(U1))["status"] == "suspended" and tg.last(U1) == T.SUSPENDED
    assert await env.disp.dispatch_once() == 0
    env.http.feeds['"roomIds"'] = [[node(BASE + 3), node(BASE + 2)]]; await env.mon.run_cycle()
    assert await db.count_queued(U1) == 0
    await tg.click(ADMIN, f"a:re:{U1}")
    assert (await db.get_user(U1))["status"] == "active"
    env.http.feeds['"roomIds"'] = [[node(BASE + 4), node(BASE + 3)]]; await env.mon.run_cycle()
    # +3 appeared while nobody entitled watched the search, +4 after: both are recent and unseen
    assert await env.disp.dispatch_once() == 2 and f"/items/{BASE + 4}" in tg.s.out[-1][1]
    # view + remove the user's link
    await tg.click(ADMIN, f"a:ul:{U1}")
    lid = (await db.user_links(U1))[0]["id"]
    assert "room_ids" in tg.last(ADMIN)
    await tg.click(ADMIN, f"a:ld:{lid}"); await tg.click(ADMIN, f"a:ldy:{lid}")
    assert await db.count_links(U1) == 0 and "admin tərəfindən silindi" in tg.last(U1)
    # statistics, health, audit, manual cycle
    await tg.click(ADMIN, "a:us:0"); await tg.click(ADMIN, "a:home")
    assert "Son 24 saat: 2" in await tg.click(ADMIN, "a:stats")
    await tg.click(ADMIN, "a:home")
    assert "Sistem vəziyyəti" in await tg.click(ADMIN, "a:health")
    await tg.click(ADMIN, "a:home")
    log = await tg.click(ADMIN, "a:audit")
    for action in ("approve", "reject", "set_plan", "set_expiry", "suspend", "reactivate", "delete_link"):
        assert action in log, action
    await tg.click(ADMIN, "a:home"); await tg.click(ADMIN, "a:run")
    for t in list(env.ctx.tasks):
        await t
    assert "Dövr bitdi" in tg.last(ADMIN)


async def test_upgrade_request_keeps_active_and_downgrade_pauses_extra_links(tg):
    db = tg.env.db
    await approve(tg, U1, "pro")
    for url in (URL_A, URL_B):
        await add_link(tg, U1, url)
    await tg.click(U1, "m:home"); await tg.click(U1, "m:subs"); await tg.click(U1, "s:p:platinum"); await tg.click(U1, "s:c:platinum")
    u = await db.get_user(U1)
    assert u["status"] == "active" and u["requested_plan"] == "platinum"   # still served while waiting
    await tg.click(ADMIN, f"a:rj:{U1}")
    assert (await db.get_user(U1))["status"] == "active"
    await tg.click(ADMIN, f"a:pl:{U1}"); await tg.click(ADMIN, f"a:ps:{U1}:basic")
    assert len(await db.monitored()) == 1 and await db.count_links(U1) == 2
    lid = (await db.user_links(U1))[1]["id"]
    await tg.click(U1, "m:home"); await tg.click(U1, "m:links")
    assert "izlənmir" in await tg.click(U1, f"l:v:{lid}")


async def test_handler_crash_reaches_admin(tg):
    await tg.text(U1, "/start")

    async def boom(*a, **k):
        raise RuntimeError("db is on fire")
    tg.env.db.count_links = boom
    await tg.press(U1, "m:status")
    assert "db is on fire" in tg.last(ADMIN) and "Xəta" in tg.last(ADMIN)
