"""User interface: main menu, status, links (add / edit / remove with confirmation),
subscription requests. A user can only ever touch their own links: every link is
loaded with (link_id AND user_id)."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from .. import texts as T
from ..bina.links import LinkError, parse_link
from ..plans import PLANS, ORDER, ST_ACTIVE, get_plan
from .common import Ctx, kb, main_kb, nav, show

router = Router(name="user")


def _max_links(u) -> int:
    p = get_plan(u["plan"])
    return p.max_links if p and u["status"] == ST_ACTIVE else 0


# ------------------------------------------------------------------ menu
@router.message(CommandStart())
async def start(m: Message, ctx: Ctx, user):
    await ctx.db.set_state(user["user_id"], None)
    await m.answer(T.welcome(user["first_name"], user["status"] == ST_ACTIVE))
    await m.answer(T.main_menu(user), reply_markup=main_kb(ctx.cfg.is_admin(user["user_id"])))


@router.callback_query(F.data == "m:home")
async def home(c: CallbackQuery, ctx: Ctx, user):
    await show(c, T.main_menu(user), main_kb(ctx.cfg.is_admin(user["user_id"])))


@router.callback_query(F.data == "m:help")
async def help_(c: CallbackQuery):
    await show(c, T.HELP, kb(nav("m:home")))


@router.callback_query(F.data == "m:status")
async def status(c: CallbackQuery, ctx: Ctx, user):
    uid = user["user_id"]
    sent = await ctx.db.sent_since(uid, int(time.time()) - 3600)
    text = T.status_card(user, await ctx.db.count_links(uid), sent,
                         await ctx.db.count_queued(uid), ctx.cfg.timezone)
    await show(c, text, kb([("🔄 Yenilə", "m:status")], nav("m:home")))


# ------------------------------------------------------------------ subscription
@router.callback_query(F.data == "m:subs")
async def subs(c: CallbackQuery, user):
    await show(c, T.subs_screen(user),
               kb(*[[(f"{PLANS[k].title} — {PLANS[k].max_links} link, {PLANS[k].ads_per_hour} elan/saat",
                      f"s:p:{k}")] for k in ORDER], nav("m:home")))


@router.callback_query(F.data.startswith("s:p:"))
async def subs_pick(c: CallbackQuery):
    plan = c.data.split(":")[2]
    if plan not in PLANS:
        return await c.answer()
    await show(c, T.subs_confirm(plan), kb([(T.BTN_SEND_REQUEST, f"s:c:{plan}")], nav("m:subs")))


@router.callback_query(F.data.startswith("s:c:"))
async def subs_send(c: CallbackQuery, ctx: Ctx, user):
    plan = c.data.split(":")[2]
    if plan not in PLANS:
        return await c.answer()
    uid = user["user_id"]
    if user["status"] == "suspended":
        return await show(c, T.SUSPENDED, kb(nav("m:subs")))
    if user["status"] == ST_ACTIVE and user["plan"] == plan and not user["requested_plan"]:
        return await show(c, f"Siz artıq <b>{T.plan_title(plan)}</b> planındasınız.", kb(nav("m:subs")))
    await ctx.db.request_plan(uid, plan)
    u = await ctx.db.get_user(uid)
    await ctx.msg.admins(T.admin_request(u), kb([(T.BTN_APPROVE, f"a:ap:{uid}"), (T.BTN_REJECT, f"a:rj:{uid}")],
                                                [("👤 İstifadəçi", f"a:u:{uid}")]))
    await show(c, T.request_sent(plan, ctx.cfg.payment_info), kb(nav("m:subs")))


# ------------------------------------------------------------------ links
async def _links_screen(event, ctx: Ctx, user, note: str = ""):
    links = await ctx.db.user_links(user["user_id"])
    rows = [[(f"{i}. {T.short_url(l['url'])}", f"l:v:{l['id']}")] for i, l in enumerate(links, 1)]
    text = (note + "\n\n" if note else "") + T.links_screen(links, _max_links(user))
    await show(event, text, kb(*rows, [(T.BTN_ADD_LINK, "l:add")], nav("m:home")))


@router.callback_query(F.data == "m:links")
async def links(c: CallbackQuery, ctx: Ctx, user):
    await _links_screen(c, ctx, user)


async def _may_add(ctx: Ctx, user) -> str | None:
    if user["status"] != ST_ACTIVE or not get_plan(user["plan"]):
        return T.NOT_ACTIVE
    n, mx = await ctx.db.count_links(user["user_id"]), _max_links(user)
    return T.LINK_LIMIT.format(n=n, max=mx) if n >= mx else None


@router.callback_query(F.data == "l:add")
async def link_add(c: CallbackQuery, ctx: Ctx, user):
    problem = await _may_add(ctx, user)
    if problem:
        return await show(c, problem, kb([(T.BTN_SUBS, "m:subs")], nav("m:links")))
    await ctx.db.set_state(user["user_id"], "add_link")
    await show(c, T.ASK_LINK, kb([(T.BTN_CANCEL, "m:links")], nav()))


async def _own_link(c: CallbackQuery, ctx: Ctx, user):
    """(link, position) of a link that belongs to this user, else shows 'not found'."""
    try:
        link_id = int(c.data.split(":")[2])
    except (IndexError, ValueError):
        link_id = -1
    links = await ctx.db.user_links(user["user_id"])            # only this user's rows
    for i, l in enumerate(links, 1):
        if l["id"] == link_id:
            return l, i
    await show(c, T.LINK_NOT_FOUND, kb(nav("m:links")))
    return None, 0


@router.callback_query(F.data.startswith("l:v:"))
async def link_view(c: CallbackQuery, ctx: Ctx, user):
    l, i = await _own_link(c, ctx, user)
    if l:
        await show(c, T.link_card(i, l["url"], l["created_at"], ctx.cfg.timezone, paused=i > _max_links(user)),
                   kb([(T.BTN_EDIT, f"l:e:{l['id']}"), (T.BTN_DELETE, f"l:d:{l['id']}")], nav("m:links")))


@router.callback_query(F.data.startswith("l:e:"))
async def link_edit_ask(c: CallbackQuery, ctx: Ctx, user):
    l, _ = await _own_link(c, ctx, user)
    if l:
        await show(c, T.CONFIRM_EDIT.format(url=T.esc(l["url"])),
                   kb([(T.BTN_YES, f"l:ey:{l['id']}"), (T.BTN_NO, f"l:v:{l['id']}")], nav(f"l:v:{l['id']}")))


@router.callback_query(F.data.startswith("l:ey:"))
async def link_edit_go(c: CallbackQuery, ctx: Ctx, user):
    l, _ = await _own_link(c, ctx, user)
    if l:
        await ctx.db.set_state(user["user_id"], f"edit_link:{l['id']}")
        await show(c, T.ASK_NEW_LINK, kb([(T.BTN_CANCEL, f"l:v:{l['id']}")], nav()))


@router.callback_query(F.data.startswith("l:d:"))
async def link_del_ask(c: CallbackQuery, ctx: Ctx, user):
    l, _ = await _own_link(c, ctx, user)
    if l:
        await show(c, T.CONFIRM_DELETE.format(url=T.esc(l["url"])),
                   kb([(T.BTN_YES, f"l:dy:{l['id']}"), (T.BTN_NO, f"l:v:{l['id']}")], nav(f"l:v:{l['id']}")))


@router.callback_query(F.data.startswith("l:dy:"))
async def link_del_go(c: CallbackQuery, ctx: Ctx, user):
    l, _ = await _own_link(c, ctx, user)
    if l:
        await ctx.db.delete_link(l["id"], user["user_id"])
        await _links_screen(c, ctx, user, T.LINK_DELETED)


# ------------------------------------------------------------------ text input
@router.message(F.text)
async def text_input(m: Message, ctx: Ctx, user):
    uid, state = user["user_id"], user["state"] or ""
    if not (state == "add_link" or state.startswith("edit_link:")):
        return await m.answer(T.UNKNOWN_INPUT, reply_markup=main_kb(ctx.cfg.is_admin(uid)))
    retry = kb([(T.BTN_CANCEL, "m:links")], nav())
    try:
        parsed = parse_link(m.text, ctx.lmap, ctx.cfg.unknown_params)
    except LinkError as e:                                   # nothing is saved or activated
        if e.kind == "invalid":
            return await m.answer(T.INVALID_LINK, reply_markup=retry)
        return await m.answer(T.UNSUPPORTED_LINK.format(detail=T.esc(e.detail)), reply_markup=retry)
    note = T.IGNORED_PARAMS.format(params=T.esc(", ".join(parsed.ignored))) if parsed.ignored else ""
    if state == "add_link":
        problem = await _may_add(ctx, user)                  # re-check: status may have changed
        if problem:
            await ctx.db.set_state(uid, None)
            return await m.answer(problem, reply_markup=kb(nav("m:links")))
        if await ctx.db.add_link(uid, parsed) is None:
            return await m.answer(T.DUPLICATE_LINK, reply_markup=retry)
        done = T.LINK_ADDED
    else:
        link_id = int(state.split(":")[1])
        if not await ctx.db.get_link(link_id, uid):
            await ctx.db.set_state(uid, None)
            return await m.answer(T.LINK_NOT_FOUND, reply_markup=kb(nav("m:links")))
        if not await ctx.db.update_link(link_id, uid, parsed):
            return await m.answer(T.DUPLICATE_LINK, reply_markup=retry)
        done = T.LINK_UPDATED
    await ctx.db.set_state(uid, None)
    await _links_screen(m, ctx, user, done + note)


@router.message()
async def other_message(m: Message, ctx: Ctx, user):
    await m.answer(T.UNKNOWN_INPUT, reply_markup=main_kb(ctx.cfg.is_admin(user["user_id"])))


@router.callback_query()
async def unknown_callback(c: CallbackQuery):
    """Anything not handled above - including admin buttons pressed by a non-admin."""
    await c.answer(T.NO_ACCESS if (c.data or "").startswith("a:") else "…", show_alert=True)
