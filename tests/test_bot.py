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
            import html as _h, re as _re                     # Telegram counts the text AFTER parsing entities
            visible = _h.unescape(_re.sub(r"<[^>]+>", "", text))
            assert len(visible) <= (1024 if isinstance(method, SendPhoto) else 4096), "Telegram would refuse this"
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
        """Tap a visible button: a key of the current keyboard-area menu, or an inline
        button under a message the bot sent on its own."""
        label = next((k for k, v in self._map(uid).items() if v == data), None)
        if label is not None:
            return await self.text(uid, label)
        assert data in self.inline(uid), f"{data!r} is not visible to {uid}: keys={self._map(uid)} inline={self.inline(uid)}"
        return await self.press(uid, data)

    async def press(self, uid, data):             # forged callback, button need not be visible
        self.n += 1
        self.s.alerts.clear()
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.n, "callback_query": {
            "id": str(self.n), "from": self._user(uid), "chat_instance": "x", "data": data,
            "message": {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": {"id": 999, "is_bot": True, "first_name": "bot"}, "text": "old"}}}))
        return self.last(uid)

    async def contact(self, uid, phone, owner=None):
        self.n += 1
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.n, "message": {
            "message_id": self.n, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": self._user(uid), "contact": {"phone_number": phone, "first_name": "x", "user_id": owner or uid}}}))
        return self.last(uid)

    async def photo(self, uid, caption=None):
        self.n += 1
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self.n, "message": {
            "message_id": self.n, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
            "from": self._user(uid), "photo": [{"file_id": "FILE1", "file_unique_id": "u", "width": 9, "height": 9}],
            **({"caption": caption} if caption else {})}}))
        return self.last(uid)

    def last(self, uid):
        return next((o[1] for o in reversed(self.s.out) if o[0] == uid), "")

    def _map(self, uid):                          # label -> action of the keyboard-area menu
        import json, sqlite3
        con = sqlite3.connect(self.env.cfg.db_path)
        row = con.execute("SELECT value FROM kv WHERE key=?", (f"kbmap:{uid}",)).fetchone()
        con.close()
        return json.loads(row[0]) if row and row[0] else {}

    def inline(self, uid):
        return [b.callback_data for o in self.s.out if o[0] == uid and hasattr(o[2], "inline_keyboard")
                for row in o[2].inline_keyboard for b in row]

    def buttons(self, uid):
        return list(self._map(uid).values())

    def labels(self, uid):
        return list(self._map(uid))

    keys = labels

    def shown_keys(self, uid):                    # what Telegram actually displays in the keyboard area
        mk = next((o[2] for o in reversed(self.s.out) if o[0] == uid and hasattr(o[2], "keyboard")), None)
        return [b.text for row in mk.keyboard for b in row] if mk else []

    async def key(self, uid, label):
        assert label in self._map(uid), f"{label!r} is not in {uid}'s keyboard: {list(self._map(uid))}"
        return await self.text(uid, label)

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
    from app.bot import admin, common, user
    for r in (admin.router, user.router, common.nav_router):          # test-only: fresh dispatcher per test
        r._parent_router = None
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
    assert tg.shown_keys(U1) == [T.BTN_STATUS, T.BTN_LINKS, T.BTN_SUBS, T.BTN_CONTACT, T.BTN_HELP, T.BTN_PHONE]
    await tg.text(ADMIN, "/start"); assert tg.keys(ADMIN)[-1] == T.BTN_ADMIN          # admins only
    # --- cannot add links before activation
    await tg.click(U1, "m:links")
    assert "aktiv abunəliyiniz olmalıdır" in await tg.click(U1, "l:add")
    # --- subscribe -> pending -> admin notified with Approve / Reject
    await tg.click(U1, "m:subs"); await tg.click(U1, "s:p:pro")
    assert "sorğunuz adminə göndərildi" in await tg.click(U1, "s:c:pro") and "Kart: 0000" in tg.last(U1)
    assert (await db.get_user(U1))["status"] == "pending"
    assert "Yeni abunəlik sorğusu" in tg.last(ADMIN) and tg.inline(ADMIN)[-3:-1] == [f"a:ap:{U1}", f"a:rj:{U1}"]
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
        assert tg.buttons(U1)[-1] == "m:home" and tg.labels(U1)[-1] == T.BTN_BACK    # one level deep: Geri only
        assert T.BTN_HOME not in tg.labels(U1)
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
    assert (await tg.text(U1, "salam")).startswith(T.UNKNOWN_INPUT) and await db.count_links(U1) == 0


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
    assert (await tg.text(U2, "/admin")).startswith(T.UNKNOWN_INPUT)
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
    assert {f"a:u:{U1}", f"a:u:{U2}", f"a:u:{ADMIN}"} <= set(tg.inline(ADMIN))
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
    for action in ("Təsdiq", "Rədd", "Plan dəyişdi", "Müddət dəyişdi", "Dayandırıldı", "Yenidən aktiv", "Link silindi"):
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


LINK2 = ("https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=2&room_ids%5B%5D=3&room_ids%5B%5D=4"
         "&has_bill_of_sale=true&has_mortgage=true")
LONG = ("https://bina.az/baki/alqi-satqi/menziller/yeni-tikili?room_ids%5B%5D=2&room_ids%5B%5D=3&room_ids%5B%5D=4"
        "&has_bill_of_sale=true&has_mortgage=true&has_repair=true&floor_first=false"
        + "".join(f"&location_ids%5B%5D={i}" for i in range(1, 201)))


async def test_very_long_link_split_by_telegram_into_several_messages(tg):
    env, db = tg.env, tg.env.db
    assert len(LONG) > 4096
    await approve(tg, U1, "pro")
    await tg.click(U1, "m:home"); await tg.click(U1, "m:links"); await tg.click(U1, "l:add")
    n = len(tg.s.out)
    await tg.text(U1, LONG[:4096])                         # part 1: bot waits silently
    assert len(tg.s.out) == n and await db.count_links(U1) == 0
    assert "Link əlavə olundu" in await tg.text(U1, LONG[4096:])
    link = (await db.user_links(U1))[0]
    assert link["url"].count("location_ids") == 200
    # every screen that shows the link stays inside Telegram's 4096-character limit (asserted in FakeSession)
    await tg.click(U1, f"l:v:{link['id']}"); await tg.click(U1, f"l:e:{link['id']}")
    await tg.click(U1, f"l:v:{link['id']}"); await tg.click(U1, f"l:d:{link['id']}")
    await tg.click(ADMIN, f"a:ul:{U1}"); await tg.click(ADMIN, f"a:ld:{link['id']}")
    # and the search is really sent to Bina.az with all 200 districts
    await env.mon.run_cycle()
    import json as _j
    assert len(_j.loads(env.http.calls[-1][1]["variables"])["filter"]["locationIds"]) == 200


async def test_pasted_link_is_accepted_without_pressing_add_first(tg):
    db = tg.env.db
    await approve(tg, U1, "pro")
    assert "Link əlavə olundu" in await tg.text(U1, LINK2)          # straight from the main menu
    await tg.click(U1, "l:add")
    assert await tg.text(U1, LINK2) == T.DUPLICATE_LINK
    await tg.text(U2, "/start")
    assert "aktiv abunəliyiniz olmalıdır" in await tg.text(U2, LINK2) and await db.count_links(U2) == 0


async def test_keyboard_area_shows_only_the_current_screens_buttons(tg):
    db = tg.env.db
    await approve(tg, U1, "pro"); await tg.text(U1, LINK2)
    lid = (await db.user_links(U1))[0]["id"]
    await tg.text(U1, "/start")
    home = [T.BTN_STATUS, T.BTN_LINKS, T.BTN_SUBS, T.BTN_CONTACT, T.BTN_HELP, T.BTN_PHONE]
    assert tg.shown_keys(U1) == home                                # no Geri / Ana səhifə on the main menu
    n = len(tg.s.out)
    assert "Linklərim" in await tg.key(U1, T.BTN_LINKS) and len(tg.s.out) == n + 1   # new message at the bottom
    assert tg.shown_keys(U1) == ["🔗 Link 1", T.BTN_ADD_LINK, T.BTN_BACK]
    assert T.BTN_STATUS not in tg.keys(U1)                          # previous menu is gone
    await tg.click(U1, f"l:v:{lid}")
    assert tg.shown_keys(U1) == [T.BTN_EDIT, T.BTN_DELETE, T.BTN_BACK, T.BTN_HOME]   # deeper: both appear
    await tg.key(U1, T.BTN_DELETE)
    assert tg.shown_keys(U1) == [T.BTN_YES, T.BTN_NO, T.BTN_BACK, T.BTN_HOME]
    assert "Link 1" in await tg.key(U1, T.BTN_BACK)                 # confirm -> link card
    assert "Linklərim" in await tg.key(U1, T.BTN_BACK)              # card -> list
    assert "Ana səhifə" in await tg.key(U1, T.BTN_BACK) and tg.shown_keys(U1) == home
    assert await db.count_links(U1) == 1
    await tg.key(U1, T.BTN_LINKS); await tg.click(U1, f"l:v:{lid}")
    assert "Ana səhifə" in await tg.key(U1, T.BTN_HOME) and tg.shown_keys(U1) == home
    # a stale key (not on the current screen) is not an action any more
    assert (await tg.text(U1, T.BTN_DELETE)).startswith(T.UNKNOWN_INPUT) and await db.count_links(U1) == 1
    # while the bot waits for a link, the only keys are Cancel / Home, and they cancel the dialogue
    await tg.key(U1, T.BTN_LINKS); await tg.key(U1, T.BTN_ADD_LINK)
    assert tg.shown_keys(U1) == [T.BTN_CANCEL, T.BTN_HOME]
    assert "Linklərim" in await tg.key(U1, T.BTN_CANCEL) and (await db.get_user(U1))["state"] is None
    # /cancel works from anywhere
    await tg.key(U1, T.BTN_ADD_LINK)
    assert (await tg.text(U1, "/cancel")).startswith(T.CANCELLED) and (await db.get_user(U1))["state"] is None
    # admin panel lives in the keyboard area too; a non-admin typing its label gets nothing
    await tg.text(ADMIN, "/start")
    assert "Admin paneli" in await tg.key(ADMIN, T.BTN_ADMIN) and "📢 Elan göndər" in tg.shown_keys(ADMIN)
    assert (await tg.text(U1, T.BTN_ADMIN)).startswith(T.UNKNOWN_INPUT)
    await tg.click(ADMIN, "a:us:0"); await tg.click(ADMIN, f"a:u:{U1}"); await tg.click(ADMIN, f"a:ex:{U1}")
    await tg.click(ADMIN, f"a:esc:{U1}")
    assert f"[<code>{U1}</code>]" in await tg.key(ADMIN, T.BTN_CANCEL)
    assert "istifadəçini seçin" in await tg.key(ADMIN, T.BTN_BACK) and "Admin paneli" in await tg.key(ADMIN, T.BTN_BACK)


async def test_contact_admin_reply_note_and_broadcast(tg):
    db = tg.env.db
    await approve(tg, U1); await approve(tg, U2)
    await tg.text(U1, "/start")
    # user -> admins (text, then a photo such as a payment receipt)
    assert "Əlaqə" in await tg.key(U1, T.BTN_CONTACT)
    assert (await tg.text(U1, "Ödəniş etdim <b>")).startswith(T.CONTACT_SENT)
    got = tg.last(ADMIN)
    assert "Yeni müraciət" in got and "Ad100" in got and "Ödəniş etdim &lt;b&gt;" in got
    await tg.key(U1, T.BTN_CONTACT); await tg.photo(U1, "qəbz")
    assert tg.s.out[-2][3] == "photo" and tg.s.out[-2][0] == ADMIN and "qəbz" in tg.s.out[-2][1]
    assert (await tg.photo(U1)).startswith(T.UNKNOWN_INPUT)          # a photo outside the dialogue is ignored
    # admin answers with the inline button under the report
    assert await tg.click(ADMIN, f"a:msg:{U1}") == T.ADMIN_MSG_ASK
    await tg.text(ADMIN, "Qəbul olundu")
    assert tg.last(U1) == T.FROM_ADMIN + "Qəbul olundu" and "Göndərildi" in tg.last(ADMIN)
    # note on the user card
    await tg.click(ADMIN, f"a:nt:{U1}")
    assert "Qeyd: kartla ödəyib 14.11" in await tg.text(ADMIN, "kartla ödəyib 14.11")
    assert (await db.get_user(U1))["note"] == "kartla ödəyib 14.11"
    await tg.click(ADMIN, f"a:nt:{U1}"); await tg.text(ADMIN, "-")
    assert (await db.get_user(U1))["note"] is None
    # broadcast: draft -> confirmation -> every ACTIVE user, nobody else
    await db.set_status(U2, "suspended")
    await tg.click(ADMIN, "a:us:0"); await tg.click(ADMIN, "a:home"); await tg.click(ADMIN, "a:bc")
    assert tg.labels(ADMIN)[:4] == ["📢 Bütün aktiv istifadəçilər", "📢 Basic istifadəçilər", "📢 Pro istifadəçilər",
                                    "📢 Platinum istifadəçilər"]
    assert await tg.click(ADMIN, "a:bct:platinum") == T.BC_EMPTY     # nobody on Platinum
    await tg.click(ADMIN, "a:bc"); await tg.click(ADMIN, "a:bct:all")
    n = len(tg.s.out)
    assert "<b>1</b> istifadəçiyə" in await tg.text(ADMIN, "Sabah texniki fasilə olacaq")
    assert all(o[0] == ADMIN for o in tg.s.out[n:])                  # nothing sent before confirming
    await tg.click(ADMIN, "a:bcy")
    for t in list(tg.env.ctx.tasks):
        await t
    assert tg.last(U1) == T.BROADCAST_HEAD + "Sabah texniki fasilə olacaq" and "Sabah" not in tg.last(U2)
    assert "göndərildi: <b>1</b>" in tg.last(ADMIN)
    # an announcement for one plan reaches only that plan's active users
    await db.set_status(U2, "active"); await db.set_user(U2, plan="pro")
    await tg.click(ADMIN, "a:bc"); await tg.click(ADMIN, "a:bct:pro")
    assert "Pro istifadəçilər" in await tg.text(ADMIN, "Pro üçün yenilik")
    await tg.click(ADMIN, "a:bcy")
    for t in list(tg.env.ctx.tasks):
        await t
    assert tg.last(U2) == T.BROADCAST_HEAD + "Pro üçün yenilik" and "Pro üçün" not in tg.last(U1)
    await tg.press(ADMIN, "a:bcy")                                   # pressing again sends nothing
    assert not tg.env.ctx.tasks
    await db.set_status(U2, "suspended")
    for forbidden in (f"a:msg:{U1}", f"a:nt:{U1}", "a:bc", "a:bct:all", "a:bcy", "a:lim", "a:lf:basic:ads_per_hour"):
        await tg.press(U2, forbidden); assert tg.s.alerts == [T.NO_ACCESS]
    actions = [r["action"] for r in await db.recent_audit(20)]
    assert {"message_user", "note", "broadcast"} <= set(actions)
    # usage statistics on the admin screen
    await tg.click(ADMIN, "a:stats")
    stats = tg.last(ADMIN)
    assert "Bu gün aktiv: 3" in stats and "Adminə mesaj: 2" in stats and "Əlaqə: 2" in stats


async def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from app.db import Database
    p = str(tmp_path / "old.sqlite3")
    d = await Database(p).open(); await d.upsert_user(5, "u", "U"); await d.close()
    con = sqlite3.connect(p); con.execute("ALTER TABLE users DROP COLUMN note"); con.commit(); con.close()
    d = await Database(p).open()
    assert (await d.get_user(5))["note"] is None and (await d.get_user(5))["username"] == "u"
    await d.close()


async def test_admin_changes_plan_limits_from_telegram(tg):
    from app import plans
    env, db = tg.env, tg.env.db
    await approve(tg, U1, "basic"); await add_link(tg, U1, URL_A)
    await tg.text(ADMIN, "/start"); await tg.key(ADMIN, T.BTN_ADMIN)
    assert "Basic</b> — 1 link, saatda maksimum 10 elan" in await tg.click(ADMIN, "a:lim")
    await tg.click(ADMIN, "a:lm:basic"); await tg.click(ADMIN, "a:lf:basic:ads_per_hour")
    assert "Düzgün rəqəm" in await tg.text(ADMIN, "çox")
    assert "Düzgün rəqəm" in await tg.text(ADMIN, "0")
    assert "Basic</b> — 1 link, saatda maksimum 3 elan" in await tg.text(ADMIN, "3")
    await tg.click(ADMIN, "a:lm:basic"); await tg.click(ADMIN, "a:lf:basic:max_links"); await tg.text(ADMIN, "2")
    # the new limits are what the user sees and what delivery enforces
    await tg.text(U1, "/start")
    card = await tg.key(U1, T.BTN_STATUS)
    assert "Linklər: 1 / 2" in card and "0 / 3" in card
    assert "Link əlavə olundu" in await tg.text(U1, URL_B)             # second link now allowed
    env.http.feeds['"roomIds"'] = [[node(BASE + 1)]]; await env.mon.run_cycle()
    env.http.feeds['"roomIds"'] = [[node(BASE + 10 + i) for i in range(6)] + [node(BASE + 1)]]; await env.mon.run_cycle()
    assert await env.disp.dispatch_once() == 3 and await db.count_queued(U1) == 3
    assert "3 elan/saat" in tg.last(U1)
    # survives a restart, and is in the audit log
    plans.reset_defaults(); assert plans.PLANS["basic"].ads_per_hour == 10
    await plans.load_limits(db)
    assert (plans.PLANS["basic"].ads_per_hour, plans.PLANS["basic"].max_links, plans.PLANS["pro"].ads_per_hour) == (3, 2, 25)
    assert [r["details"] for r in await db.recent_audit(5) if r["action"] == "set_limit"] == [
        "basic.max_links: 1 -> 2", "basic.ads_per_hour: 10 -> 3"]


async def test_users_list_details_clickable_links_and_phone(tg):
    db = tg.env.db
    await approve(tg, U1, "pro"); await add_link(tg, U1, URL_A); await tg.text(U1, LONG[:4096]); await tg.text(U1, LONG[4096:])
    # phone: only the user's OWN contact, shared with Telegram's button, is stored
    await tg.text(U1, "/start")
    assert (await tg.contact(U1, "994501112233", owner=U2)).startswith(T.PHONE_NOT_OWN)
    assert (await db.get_user(U1))["phone"] is None
    assert (await tg.contact(U1, "994501112233")).startswith(T.PHONE_SAVED)
    assert (await db.get_user(U1))["phone"] == "+994501112233" and T.BTN_PHONE not in tg.shown_keys(U1)
    assert "📞 Telefon: +994501112233" in await tg.key(U1, T.BTN_STATUS)
    # user's own list: short clickable names
    await tg.key(U1, T.BTN_BACK)
    mine = await tg.key(U1, T.BTN_LINKS)
    assert '<a href="https://bina.az/baki/alqi-satqi/menziller?room_ids%5B%5D=3">Link 1</a>' in mine and ">Link 2</a>" in mine
    assert tg.shown_keys(U1)[:2] == ["🔗 Link 1", "🔗 Link 2"]
    # admin: users page = details as text + one inline button per user
    await tg.text(ADMIN, "/start"); await tg.key(ADMIN, T.BTN_ADMIN)
    n = len(tg.s.out)
    await tg.click(ADMIN, "a:us:0")
    header, details = tg.s.out[n], tg.s.out[n + 1]
    assert "İstifadəçilər" in header[1] and hasattr(header[2], "keyboard")
    for part in ("Ad100</b> — @user100", f"<code>{U1}</code> · ✅ Aktiv · Pro", "📞 +994501112233 · 🔗 2 link"):
        assert part in details[1], part
    assert [b.text for row in details[2].inline_keyboard for b in row] == [f"@user{U1} · {U1}", f"@user{ADMIN} · {ADMIN}"]
    card = await tg.click(ADMIN, f"a:u:{U1}")                       # opens as a new message
    assert "İstifadəçi adı: @user100" in card and "Telefon: +994501112233" in card
    links = await tg.click(ADMIN, f"a:ul:{U1}")
    assert ">Link 1</a>" in links and ">Link 2</a>" in links and links.count("location_ids") == 200   # full URL only inside href
    assert tg.shown_keys(ADMIN)[:2] == ["🗑 Link 1", "🗑 Link 2"]
