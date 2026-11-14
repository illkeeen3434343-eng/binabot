"""Administrator interface. The whole router is filtered by the predefined admin
IDs, so every admin action (button or text) is checked against the sender's
Telegram ID before any handler runs. Important actions go to the audit log."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message

from .. import texts as T
from ..plans import ORDER, PLANS, ST_ACTIVE, ST_EXPIRED, ST_PENDING, ST_REJECTED, ST_SUSPENDED
from .common import KEYBOARD_ACTIONS, Ctx, ack, kb, nav, show

PAGE = 8
DURATIONS = [(30, "30 gün"), (90, "90 gün"), (180, "180 gün"), (365, "365 gün")]


class IsAdmin(BaseFilter):
    async def __call__(self, event, ctx: Ctx) -> bool:
        return bool(event.from_user) and ctx.cfg.is_admin(event.from_user.id)


class AdminState(BaseFilter):
    async def __call__(self, event, user) -> bool:
        return (user["state"] or "").startswith("adm_") and getattr(event, "text", None) not in KEYBOARD_ACTIONS


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
    text = (f"🛠 <b>Admin panel</b>\n\nİstifadəçilər: {sum(sc.values())}\n"
            f"Aktiv: {sc.get(ST_ACTIVE, 0)} | Gözləyən sorğu: {pend}\n"
            f"Növbədə elan: {await ctx.db.count_queued()}")
    await show(event, text, kb([(f"⏳ Sorğular ({pend})", "a:pend"), ("👥 İstifadəçilər", "a:us:0")],
                               [("📈 Statistika", "a:stats"), ("🩺 Sistem vəziyyəti", "a:health")],
                               [("▶️ Monitorinqi indi başlat", "a:run"), ("📜 Audit", "a:audit")], nav()))


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
    rows = [[(f"{u['first_name'] or u['user_id']} → {T.plan_title(u['requested_plan'])}", f"a:u:{u['user_id']}")]
            for u in reqs[:30]]
    await show(c, "⏳ <b>Gözləyən sorğular</b>" + ("" if reqs else "\n\nSorğu yoxdur."),
               kb(*rows, nav("a:home")))


@router.callback_query(F.data.startswith("a:us:"))
async def users(c: CallbackQuery, ctx: Ctx):
    page = max(0, _uid(c))
    total = await ctx.db.count_users()
    us = await ctx.db.list_users(page * PAGE, PAGE)
    icon = {"active": "✅", "pending": "⏳", "rejected": "❌", "suspended": "⏸", "expired": "⌛", "new": "▫️"}
    rows = [[(f"{icon.get(u['status'], '')} {u['first_name'] or '—'} · {T.plan_title(u['plan'])} · {u['user_id']}",
              f"a:u:{u['user_id']}")] for u in us]
    pager = ([("◀️", f"a:us:{page - 1}")] if page else []) + \
            ([("▶️", f"a:us:{page + 1}")] if (page + 1) * PAGE < total else [])
    pages = max(1, -(-total // PAGE))
    await show(c, f"👥 <b>İstifadəçilər</b> — cəmi {total} (səhifə {page + 1}/{pages})",
               kb(*rows, pager, nav("a:home")))


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
                           f"plan={plan} expires={T.fmt_dt(expires_at, ctx.cfg.timezone)}")
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
    await _audit(ctx, c, "reject", uid, f"requested={u['requested_plan']}")
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


@router.message(AdminState(), F.text)
async def date_input(m: Message, ctx: Ctx, user):
    _, mode, uid = user["state"].split(":")
    uid, ts = int(uid), None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = dt.datetime.strptime(m.text.strip(), fmt)
            ts = int(d.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo(ctx.cfg.timezone)).timestamp())
            break
        except ValueError:
            continue
    if ts is None or ts <= time.time():
        return await m.answer(T.BAD_DATE, reply_markup=kb([(T.BTN_CANCEL, f"a:u:{uid}")]))
    await ctx.db.set_state(user["user_id"], None)
    if mode == "adc":
        await _approve(m, ctx, user["user_id"], uid, ts)
    else:
        await _set_expiry(m, ctx, user["user_id"], uid, ts)


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
    lines = [f"{i}. <code>{T.show_url(l['url'], 350)}</code>" + (f"\n   ⚠️ {T.esc(l['last_error'])}" if l["last_error"] else "")
             for i, l in enumerate(links, 1)]
    rows = [[(f"🗑 {i}. {T.short_url(l['url'], 30)}", f"a:ld:{l['id']}")] for i, l in enumerate(links, 1)]
    await show(c, "🔗 <b>İstifadəçinin linkləri</b>\n\n" + ("\n".join(lines) or "Link yoxdur."),
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
    text = "📜 <b>Audit (son 15)</b>\n\n" + ("\n".join(
        f"• {T.fmt_dt(r['ts'], ctx.cfg.timezone)} admin <code>{r['admin_id']}</code> — {T.esc(r['action'])}"
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
