"""Administrator interface. The whole router is filtered by the predefined admin
IDs, so every admin action (button or text) is checked against the sender's
Telegram ID before any handler runs. Important actions go to the audit log."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message

from .. import texts as T
from ..plans import LIMIT_RANGE, ORDER, PLANS, set_limit, ST_ACTIVE, ST_EXPIRED, ST_PENDING, ST_REJECTED, ST_SUSPENDED
from .common import Ctx, ack, kb, nav, show, show_inline

PAGE = 8
DURATIONS = [(30, "30 gün"), (90, "90 gün"), (180, "180 gün"), (365, "365 gün")]


class IsAdmin(BaseFilter):
    async def __call__(self, event, ctx: Ctx) -> bool:
        return bool(event.from_user) and ctx.cfg.is_admin(event.from_user.id)


class AdminState(BaseFilter):
    async def __call__(self, event, user) -> bool:
        return (user["state"] or "").startswith("adm_")


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _uid(c: CallbackQuery, i=2) -> int:
    return int(c.data.split(":")[i])


async def _audit(ctx: Ctx, c, action, target=None, details=""):
    await ctx.db.add_audit(c.from_user.id, action, target, details)


# ------------------------------------------------------------------ panel
async def _panel(event, ctx: Ctx):
    sc = await ctx.db.status_counts()
    pend = len(await ctx.db.pending_requests())
    text = (f"🛠 <b>Admin paneli</b>\n\nİstifadəçilər: {sum(sc.values())}\n"
            f"Aktiv: {sc.get(ST_ACTIVE, 0)} | Gözləyən sorğu: {pend}\n"
            f"Növbədə elan: {await ctx.db.count_queued()}")
    await show(event, text, kb([(f"⏳ Sorğular ({pend})", "a:pend"), ("👥 İstifadəçilər", "a:us:0")],
                               [("📈 Statistika", "a:stats"), ("🩺 Sistem vəziyyəti", "a:health")],
                               [("▶️ Monitorinqi başlat", "a:run"), ("📜 Jurnal", "a:audit")],
                               [("📢 Elan göndər", "a:bc"), ("⚙️ Plan limitləri", "a:lim")], nav("m:home")))


@router.message(Command("admin"))
async def admin_cmd(m: Message, ctx: Ctx, user):
    await ctx.db.set_state(user["user_id"], None)
    await _panel(m, ctx)


@router.callback_query(F.data == "a:home")
async def admin_home(c: CallbackQuery, ctx: Ctx):
    await _panel(c, ctx)


@router.callback_query(F.data == "a:pend")
async def pending(c: CallbackQuery, ctx: Ctx):
    reqs = await ctx.db.pending_requests()
    rows = [[(f"{u['first_name'] or '—'} → {T.plan_title(u['requested_plan'])} · {u['user_id']}", f"a:u:{u['user_id']}")]
            for u in reqs[:12]]
    await show(c, "⏳ <b>Gözləyən sorğular</b>" + ("" if reqs else "\n\nSorğu yoxdur."),
               kb(*rows, nav("a:home")))


@router.callback_query(F.data.startswith("a:us:"))
async def users(c: CallbackQuery, ctx: Ctx):
    """Header + pager in the keyboard area, then the details as a message with one
    inline button per user (pressing it opens the user's card as a new message)."""
    page = max(0, _uid(c))
    total = await ctx.db.count_users()
    us = await ctx.db.list_users(page * PAGE, PAGE)
    pager = ([("◀️ Əvvəlki", f"a:us:{page - 1}")] if page else []) + \
            ([("Növbəti ▶️", f"a:us:{page + 1}")] if (page + 1) * PAGE < total else [])
    pages = max(1, -(-total // PAGE))
    await show(c, f"👥 <b>İstifadəçilər</b> — cəmi {total} (səhifə {page + 1}/{pages})", kb(pager, nav("a:home")))
    counts = await ctx.db.link_counts([u["user_id"] for u in us])
    buttons = [[(T.user_button(u), f"a:u:{u['user_id']}")] for u in us]
    await show_inline(c, T.admin_users_list(us, counts, page * PAGE, ctx.cfg.timezone)
                      + ("\n\nƏtraflı baxmaq və idarə etmək üçün istifadəçini seçin 👇" if us else ""), kb(*buttons))


# ------------------------------------------------------------------ one user
async def _card(event, ctx: Ctx, uid: int, note: str = ""):
    u = await ctx.db.get_user(uid)
    if not u:
        return await show(event, "İstifadəçi tapılmadı.", kb(nav("a:us:0")))
    sent = await ctx.db.sent_since(uid, int(time.time()) - 3600)
    text = T.admin_user_card(u, await ctx.db.count_links(uid), sent, await ctx.db.count_queued(uid),
                             ctx.cfg.timezone)
    rows = []
    if u["requested_plan"]:
        rows.append([(T.BTN_APPROVE, f"a:ap:{uid}"), (T.BTN_REJECT, f"a:rj:{uid}")])
    rows.append([("📦 Plan", f"a:pl:{uid}"), ("📅 Müddət", f"a:ex:{uid}")])
    rows.append([("✉️ Mesaj yaz", f"a:msg:{uid}"), ("📝 Qeyd", f"a:nt:{uid}")])
    if u["status"] == ST_ACTIVE:
        rows.append([("⏸ Dayandır", f"a:su:{uid}"), ("🔗 Linklər", f"a:ul:{uid}")])
    elif u["status"] == ST_SUSPENDED:
        rows.append([("▶️ Yenidən aktiv et", f"a:re:{uid}"), ("🔗 Linklər", f"a:ul:{uid}")])
    else:
        rows.append([("🔗 Linklər", f"a:ul:{uid}")])
    await show(event, (note + "\n\n" if note else "") + text, kb(*rows, nav("a:us:0")))


@router.callback_query(F.data.startswith("a:u:"))
async def user_card(c: CallbackQuery, ctx: Ctx):
    await _card(c, ctx, _uid(c))


def _duration_kb(prefix: str, uid: int, back: str):
    return kb([(t, f"{prefix}:{uid}:{d}") for d, t in DURATIONS[:2]],
              [(t, f"{prefix}:{uid}:{d}") for d, t in DURATIONS[2:]],
              [("✍️ Tarixi əl ilə yaz", f"{prefix}c:{uid}")], nav(back))


async def _approve(event, ctx: Ctx, admin_id: int, uid: int, expires_at: int):
    u = await ctx.db.get_user(uid)
    if not u:
        return await show(event, "İstifadəçi tapılmadı.", kb(nav("a:home")))
    plan = u["requested_plan"] or u["plan"] or "basic"
    await ctx.db.activate(uid, plan, expires_at)
    await ctx.db.add_audit(admin_id, "approve", uid,
                           f"plan: {T.plan_title(plan)}, bitmə: {T.fmt_dt(expires_at, ctx.cfg.timezone)}")
    await ctx.msg.user(uid, T.approved(plan, expires_at, ctx.cfg.timezone))
    await _card(event, ctx, uid, "✅ Təsdiqləndi.")


@router.callback_query(F.data.startswith("a:ap:"))
async def approve_ask(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    u = await ctx.db.get_user(uid)
    if not u:
        return await ack(c, "İstifadəçi tapılmadı.", True)
    plan = u["requested_plan"] or u["plan"] or "basic"
    await show(c, f"👤 {T.user_line(u)}\nPlan: <b>{T.plan_title(plan)}</b>\n\n{T.CHOOSE_DURATION}",
               _duration_kb("a:ad", uid, f"a:u:{uid}"))


@router.callback_query(F.data.startswith("a:ad:"))
async def approve_days(c: CallbackQuery, ctx: Ctx):
    await _approve(c, ctx, c.from_user.id, _uid(c), int(time.time()) + _uid(c, 3) * 86400)


@router.callback_query(F.data.startswith("a:adc:") | F.data.startswith("a:esc:"))
async def ask_date(c: CallbackQuery, ctx: Ctx):
    mode, uid = c.data.split(":")[1], _uid(c)
    await ctx.db.set_state(c.from_user.id, f"adm_date:{mode}:{uid}")
    await show(c, T.ASK_DATE, kb([(T.BTN_CANCEL, f"a:u:{uid}")], nav(f"a:u:{uid}")))


@router.callback_query(F.data.startswith("a:rj:"))
async def reject(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    u = await ctx.db.get_user(uid)
    if not u or not (u["requested_plan"] or u["status"] == ST_PENDING):
        return await _card(c, ctx, uid, "Gözləyən sorğu yoxdur.")
    await ctx.db.set_user(uid, requested_plan=None, requested_at=None)
    if u["status"] == ST_PENDING:
        await ctx.db.set_status(uid, ST_REJECTED)
    await _audit(ctx, c, "reject", uid, f"sorğu: {T.plan_title(u['requested_plan'])}")
    await ctx.msg.user(uid, T.REJECTED)
    await _card(c, ctx, uid, "❌ Rədd edildi.")


@router.callback_query(F.data.startswith("a:pl:"))
async def plan_ask(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    await show(c, T.CHOOSE_PLAN, kb([(PLANS[k].title, f"a:ps:{uid}:{k}") for k in ORDER], nav(f"a:u:{uid}")))


@router.callback_query(F.data.startswith("a:ps:"))
async def plan_set(c: CallbackQuery, ctx: Ctx):
    uid, plan = _uid(c), c.data.split(":")[3]
    u = await ctx.db.get_user(uid)
    if not u or plan not in PLANS:
        return await ack(c)
    if u["status"] != ST_ACTIVE:                 # no running subscription: plan + period = activation
        await ctx.db.set_user(uid, requested_plan=plan, requested_at=int(time.time()))
        return await show(c, f"Plan: <b>{T.plan_title(plan)}</b>\n\n{T.CHOOSE_DURATION}",
                          _duration_kb("a:ad", uid, f"a:u:{uid}"))
    await ctx.db.set_user(uid, plan=plan, requested_plan=None, requested_at=None)
    await _audit(ctx, c, "set_plan", uid, f"{u['plan']} -> {plan}")
    await ctx.msg.user(uid, T.plan_changed(plan))
    await _card(c, ctx, uid, "📦 Plan dəyişdirildi.")     # new limits apply immediately


async def _set_expiry(event, ctx: Ctx, admin_id: int, uid: int, expires_at: int):
    u = await ctx.db.get_user(uid)
    if not u:
        return await show(event, "İstifadəçi tapılmadı.", kb(nav("a:home")))
    fields = {"expires_at": expires_at, "last_reminder_day": None}
    if u["status"] == ST_EXPIRED and u["plan"]:
        fields["status"] = ST_ACTIVE               # extending an expired subscription renews it
    await ctx.db.set_user(uid, **fields)
    await ctx.db.add_audit(admin_id, "set_expiry", uid, T.fmt_dt(expires_at, ctx.cfg.timezone))
    await ctx.msg.user(uid, T.expiry_changed(expires_at, ctx.cfg.timezone))
    await _card(event, ctx, uid, "📅 Bitmə tarixi yeniləndi.")


@router.callback_query(F.data.startswith("a:ex:"))
async def expiry_ask(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    await show(c, "Bu gündən etibarən yeni müddət:", _duration_kb("a:es", uid, f"a:u:{uid}"))


@router.callback_query(F.data.startswith("a:es:"))
async def expiry_days(c: CallbackQuery, ctx: Ctx):
    await _set_expiry(c, ctx, c.from_user.id, _uid(c), int(time.time()) + _uid(c, 3) * 86400)


async def _date_input(m: Message, ctx: Ctx, user, mode: str, uid: int):
    ts = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = dt.datetime.strptime(m.text.strip(), fmt)
            ts = int(d.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo(ctx.cfg.timezone)).timestamp())
            break
        except ValueError:
            continue
    if ts is None or ts <= time.time():
        return await show(m, T.BAD_DATE, kb([(T.BTN_CANCEL, f"a:u:{uid}")]))
    await ctx.db.set_state(user["user_id"], None)
    if mode == "adc":
        await _approve(m, ctx, user["user_id"], uid, ts)
    else:
        await _set_expiry(m, ctx, user["user_id"], uid, ts)


# ------------------------------------------------------------------ messages / notes / broadcast
@router.callback_query(F.data.startswith("a:msg:"))
async def message_ask(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    await ctx.db.set_state(c.from_user.id, f"adm_msg:{uid}")
    await show(c, T.ADMIN_MSG_ASK, kb([(T.BTN_CANCEL, f"a:u:{uid}")]))


@router.callback_query(F.data.startswith("a:nt:"))
async def note_ask(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    await ctx.db.set_state(c.from_user.id, f"adm_note:{uid}")
    await show(c, T.NOTE_ASK, kb([(T.BTN_CANCEL, f"a:u:{uid}")]))


@router.callback_query(F.data == "a:bc")
async def broadcast_menu(c: CallbackQuery, ctx: Ctx):
    await show(c, T.BC_MENU, kb(*[[(f"📢 {T.BC_TARGETS[k]}", f"a:bct:{k}")] for k in ("all", *ORDER)], nav("a:home")))


async def _bc_ids(ctx: Ctx, target: str):
    return await ctx.db.active_user_ids(None if target == "all" else target)


@router.callback_query(F.data.startswith("a:bct:"))
async def broadcast_ask(c: CallbackQuery, ctx: Ctx):
    target = c.data.split(":")[2]
    if target not in T.BC_TARGETS:
        return await ack(c)
    n = len(await _bc_ids(ctx, target))
    if not n:
        return await show(c, T.BC_EMPTY, kb(nav("a:bc")))
    await ctx.db.set_state(c.from_user.id, f"adm_bc:{target}")
    await show(c, T.BROADCAST_ASK.format(target=T.BC_TARGETS[target], n=n), kb([(T.BTN_CANCEL, "a:bc")]))


@router.callback_query(F.data == "a:bcy")
async def broadcast_go(c: CallbackQuery, ctx: Ctx):
    admin_id = c.from_user.id
    raw = await ctx.db.kv_get(f"bc:{admin_id}", "")
    if not raw:
        return await _panel(c, ctx)
    await ctx.db.kv_set(f"bc:{admin_id}", "")
    draft = json.loads(raw)
    targets = await _bc_ids(ctx, draft["target"])
    await _audit(ctx, c, "broadcast", None, f"{T.BC_TARGETS[draft['target']]}: {len(targets)}")

    async def job():
        sent = 0
        for uid in targets:
            sent += await ctx.msg.deliver(uid, draft["kind"], draft["payload"], T.BROADCAST_HEAD)
            await asyncio.sleep(0.05)                 # stay under Telegram's rate limits
        await ctx.msg.user(admin_id, f"📢 {T.BC_TARGETS[draft['target']]} — göndərildi: <b>{sent}</b>"
                           + (f" · çatdırılmadı: <b>{len(targets) - sent}</b>" if sent < len(targets) else ""))
    t = asyncio.create_task(job())
    ctx.tasks.add(t)
    t.add_done_callback(ctx.tasks.discard)
    await _panel(c, ctx)


async def _relay(m: Message, ctx: Ctx, user, kind: str, payload: str):
    """Text or photo typed by an admin while a message / announcement dialogue is open."""
    admin_id, state = user["user_id"], user["state"]
    if state.startswith("adm_bc:"):
        target = state.split(":")[1]
        await ctx.db.kv_set(f"bc:{admin_id}", json.dumps({"kind": kind, "payload": payload, "target": target}))
        await ctx.db.set_state(admin_id, None)
        preview = "🖼 Şəkil" + (f": {payload.partition('|')[2][:300]}" if payload.partition("|")[2] else "") \
            if kind == "photo" else payload[:600]
        n = len(await _bc_ids(ctx, target))
        return await show(m, T.BROADCAST_CONFIRM.format(n=n, target=T.BC_TARGETS[target], preview=preview),
                          kb([("✅ Bəli, göndər", "a:bcy"), (T.BTN_NO, "a:bc")]))
    uid = int(state.split(":")[1])
    await ctx.db.set_state(admin_id, None)
    ok = await ctx.msg.deliver(uid, kind, payload, T.FROM_ADMIN)
    await ctx.db.add_audit(admin_id, "message_user", uid, "şəkil" if kind == "photo" else "mətn")
    await _card(m, ctx, uid, "✅ Göndərildi." if ok else "⚠️ Çatdırılmadı (istifadəçi botu bloklamış ola bilər).")


# ------------------------------------------------------------------ plan limits
@router.callback_query(F.data == "a:lim")
async def limits(c: CallbackQuery, ctx: Ctx, note: str = ""):
    await show(c, (note + "\n\n" if note else "") + T.limits_screen(),
               kb([(f"⚙️ {PLANS[k].title}", f"a:lm:{k}") for k in ORDER], nav("a:home")))


@router.callback_query(F.data.startswith("a:lm:"))
async def limits_plan(c: CallbackQuery, ctx: Ctx):
    key = c.data.split(":")[2]
    if key not in PLANS:
        return await ack(c)
    p = PLANS[key]
    await show(c, f"⚙️ <b>{p.title}</b>\n\nSaatlıq elan limiti: <b>{p.ads_per_hour}</b>\nMaksimum link sayı: <b>{p.max_links}</b>",
               kb([("📨 Saatlıq elan limiti", f"a:lf:{key}:ads_per_hour")], [("🔗 Link sayı", f"a:lf:{key}:max_links")],
                  nav("a:lim")))


@router.callback_query(F.data.startswith("a:lf:"))
async def limits_field(c: CallbackQuery, ctx: Ctx):
    _, _, key, field = c.data.split(":")
    if key not in PLANS or field not in LIMIT_RANGE:
        return await ack(c)
    lo, hi = LIMIT_RANGE[field]
    await ctx.db.set_state(c.from_user.id, f"adm_lim:{key}:{field}")
    await show(c, T.LIMITS_ASK[field].format(plan=PLANS[key].title, lo=lo, hi=hi), kb([(T.BTN_CANCEL, f"a:lm:{key}")]))


async def _limit_input(m: Message, ctx: Ctx, user, key: str, field: str):
    lo, hi = LIMIT_RANGE[field]
    old = getattr(PLANS[key], field)
    try:
        await set_limit(ctx.db, key, field, int(m.text.strip()))
    except ValueError:
        return await show(m, T.LIMITS_BAD.format(lo=lo, hi=hi), kb([(T.BTN_CANCEL, f"a:lm:{key}")]))
    await ctx.db.set_state(user["user_id"], None)
    await ctx.db.add_audit(user["user_id"], "set_limit", None, f"{key}.{field}: {old} -> {getattr(PLANS[key], field)}")
    await show(m, "✅ Limit yeniləndi.\n\n" + T.limits_screen(),
               kb([(f"⚙️ {PLANS[k].title}", f"a:lm:{k}") for k in ORDER], nav("a:home")))


@router.message(AdminState(), F.photo)
async def admin_photo(m: Message, ctx: Ctx, user):
    if user["state"].startswith(("adm_msg:", "adm_bc:")):
        return await _relay(m, ctx, user, "photo", f"{m.photo[-1].file_id}|{T.esc(m.caption or '')}")
    await show(m, "Burada şəkil qəbul olunmur — mətn göndərin.", kb([(T.BTN_CANCEL, "a:home")]))


@router.message(AdminState(), F.text)
async def admin_text(m: Message, ctx: Ctx, user):
    parts = user["state"].split(":")
    if parts[0] == "adm_date":
        return await _date_input(m, ctx, user, parts[1], int(parts[2]))
    if parts[0] == "adm_lim":
        return await _limit_input(m, ctx, user, parts[1], parts[2])
    if parts[0] == "adm_note":
        uid = int(parts[1])
        note = None if m.text.strip() == "-" else m.text.strip()[:300]
        await ctx.db.set_user(uid, note=note)
        await ctx.db.set_state(user["user_id"], None)
        await ctx.db.add_audit(user["user_id"], "note", uid, note or "(silindi)")
        return await _card(m, ctx, uid, "📝 Qeyd yadda saxlandı.")
    await _relay(m, ctx, user, "text", T.esc(m.text))


@router.callback_query(F.data.startswith("a:su:"))
async def suspend(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    u = await ctx.db.get_user(uid)
    if u and u["status"] == ST_ACTIVE:
        await ctx.db.set_status(uid, ST_SUSPENDED)
        await _audit(ctx, c, "suspend", uid)
        await ctx.msg.user(uid, T.SUSPENDED)
    await _card(c, ctx, uid, "⏸ Dayandırıldı.")


@router.callback_query(F.data.startswith("a:re:"))
async def reactivate(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    u = await ctx.db.get_user(uid)
    if not u or u["status"] != ST_SUSPENDED:
        return await _card(c, ctx, uid)
    if not u["plan"] or not u["expires_at"] or u["expires_at"] <= time.time():
        await ctx.db.set_status(uid, ST_EXPIRED)
        await _audit(ctx, c, "reactivate_expired", uid)
        return await _card(c, ctx, uid, "⌛ Müddət bitib — əvvəlcə «📅 Müddət» ilə yeni tarix təyin edin.")
    await ctx.db.set_status(uid, ST_ACTIVE)
    await _audit(ctx, c, "reactivate", uid)
    await ctx.msg.user(uid, T.REACTIVATED)
    await _card(c, ctx, uid, "▶️ Aktiv edildi.")


# ------------------------------------------------------------------ user links
@router.callback_query(F.data.startswith("a:ul:"))
async def user_links(c: CallbackQuery, ctx: Ctx):
    uid = _uid(c)
    links = await ctx.db.user_links(uid)
    lines = [f"{i}. {T.link_anchor(i, l['url'])}" + (f" — ⚠️ {T.esc(l['last_error'])}" if l["last_error"] else "")
             for i, l in enumerate(links, 1)]
    rows = [[(f"🗑 Link {i}", f"a:ld:{l['id']}")] for i, l in enumerate(links, 1)]
    await show(c, "🔗 <b>İstifadəçinin linkləri</b>\n\n" + ("\n".join(lines) + "\n\nLinki açmaq üçün adına toxunun."
                                                            if lines else "Link yoxdur."),
               kb(*rows, nav(f"a:u:{uid}")))


@router.callback_query(F.data.startswith("a:ld:"))
async def link_del_ask(c: CallbackQuery, ctx: Ctx):
    l = await ctx.db.get_link(_uid(c))
    if not l:
        return await ack(c, T.LINK_NOT_FOUND, True)
    await show(c, T.CONFIRM_DELETE.format(url=T.show_url(l["url"])),
               kb([(T.BTN_YES, f"a:ldy:{l['id']}"), (T.BTN_NO, f"a:ul:{l['user_id']}")], nav(f"a:ul:{l['user_id']}")))


@router.callback_query(F.data.startswith("a:ldy:"))
async def link_del_go(c: CallbackQuery, ctx: Ctx):
    l = await ctx.db.get_link(_uid(c))
    if not l:
        return await ack(c, T.LINK_NOT_FOUND, True)
    await ctx.db.delete_link(l["id"])
    await _audit(ctx, c, "delete_link", l["user_id"], l["url"][:300])
    await ctx.msg.user(l["user_id"], T.LINK_REMOVED_BY_ADMIN.format(url=T.show_url(l["url"])))
    await _card(c, ctx, l["user_id"], T.LINK_DELETED)


# ------------------------------------------------------------------ stats / health / run
@router.callback_query(F.data == "a:stats")
async def stats(c: CallbackQuery, ctx: Ctx):
    now = int(time.time())
    sc = await ctx.db.status_counts()
    d1, d7, dall = (await ctx.db.delivery_stats(now - 86400), await ctx.db.delivery_stats(now - 7 * 86400),
                    await ctx.db.delivery_stats(0))
    top = await ctx.db.top_receivers(now - 86400)
    text = ("📈 <b>Statistika</b>\n\n<b>İstifadəçilər</b>\n"
            + "\n".join(f"{T.STATUS.get(k, k)}: {v}" for k, v in sorted(sc.items()))
            + f"\n\n<b>Elan bildirişləri</b>\nSon 24 saat: {d1.get('sent', 0)}\nSon 7 gün: {d7.get('sent', 0)}\n"
              f"Cəmi: {dall.get('sent', 0)}\nNövbədə: {await ctx.db.count_queued()}\n"
              f"Vaxtı keçən (24 s): {d1.get('expired', 0)} | Silinmiş elan: {d1.get('gone', 0)} | "
              f"Uğursuz: {d1.get('failed', 0)}")
    if top:
        text += "\n\n<b>Ən çox alanlar (24 s)</b>\n" + "\n".join(f"<code>{r['user_id']}</code>: {r['n']}" for r in top)
    us = await ctx.db.usage_summary()
    text += (f"\n\n<b>İstifadə (son 7 gün)</b>\nBu gün aktiv: {us['active_today']} | 7 gündə aktiv: {us['active_period']}")
    if us["actions"]:
        text += "\n" + "\n".join(f"• {T.esc(T.ACTION_NAMES.get(r['action'], r['action']))}: {r['n']}" for r in us["actions"])
    if us["users"]:
        text += "\n<b>Ən aktiv istifadəçilər</b>\n" + "\n".join(f"<code>{r['user_id']}</code>: {r['n']}" for r in us["users"])
    await show(c, text, kb([("🔄 Yenilə", "a:stats")], nav("a:home")))


@router.callback_query(F.data == "a:health")
async def health(c: CallbackQuery, ctx: Ctx):
    tz, now = ctx.cfg.timezone, time.time()
    last = await ctx.db.last_cycle()
    until = await ctx.monitor.blocked_until()
    mon = await ctx.db.monitored()
    cs = await ctx.db.cycle_stats(int(now) - 86400)
    text = ("🩺 <b>Sistem vəziyyəti</b>\n\n"
            f"İzlənən unikal axtarış: {len(mon)} ({sum(len(l) for _, l in mon)} link)\n"
            f"Son dövr: {T.fmt_dt(last['started_at'], tz) if last else '—'}"
            + (f" — uğurlu {last['searches_ok']}, uğursuz {last['searches_failed']}, yeni {last['new_ads']}"
               + (f", qeyd: {T.esc(last['note'])}" if last["note"] else "") if last else "")
            + f"\n24 saatda dövr: {cs['n']} (problemli: {cs['bad'] or 0})\n"
            + (f"⛔️ Bina.az fasiləsi: {T.fmt_dt(until, tz)}-dək\n" if until > now else "Bina.az: fasilə yoxdur ✅\n"))
    errs = await ctx.db.recent_errors(8)
    text += "\n<b>Son xətalar</b>\n" + ("\n".join(
        f"• {T.fmt_dt(e['ts'], tz)} {T.esc(e['source'])} [{T.esc(e['code'] or '')}] {T.esc((e['message'] or '')[:90])}"
        for e in errs) or "Xəta yoxdur ✅")
    await show(c, text[:4000], kb([("🔄 Yenilə", "a:health")], nav("a:home")))


@router.callback_query(F.data == "a:audit")
async def audit(c: CallbackQuery, ctx: Ctx):
    rows = await ctx.db.recent_audit(15)
    text = "📜 <b>Əməliyyat jurnalı (son 15)</b>\n\n" + ("\n".join(
        f"• {T.fmt_dt(r['ts'], ctx.cfg.timezone)} admin <code>{r['admin_id']}</code> — "
        f"{T.esc(T.AUDIT_NAMES.get(r['action'], r['action']))}"
        f" → <code>{r['target_user'] or '—'}</code> {T.esc((r['details'] or '')[:80])}" for r in rows) or "Boşdur.")
    await show(c, text[:4000], kb(nav("a:home")))


@router.callback_query(F.data == "a:run")
async def run_now(c: CallbackQuery, ctx: Ctx):
    await _audit(ctx, c, "manual_cycle")
    admin_id = c.from_user.id

    async def job():
        r = await ctx.monitor.run_cycle(trigger="manual", force=True)
        if r.get("skipped"):
            text = "ℹ️ Monitorinq dövrü artıq işləyir."
        else:
            text = (f"▶️ Dövr bitdi: axtarış {r['searches']}, uğurlu {r['ok']}, uğursuz {r['failed']}, "
                    f"növbəyə düşən yeni elan {r['queued']}" + (f"\nQeyd: {T.esc(str(r['note']))}" if r["note"] else ""))
        await ctx.msg.user(admin_id, text)
    t = asyncio.create_task(job())
    ctx.tasks.add(t)
    t.add_done_callback(ctx.tasks.discard)
    await ack(c, "▶️ Monitorinq dövrü başladıldı.")
