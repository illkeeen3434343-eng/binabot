"""Shared bot plumbing: context, screens, the keyboard-area menu, registration.

Screens are declared once as (text, buttons) with `kb(...)`. `show()` puts those
buttons into the phone's KEYBOARD AREA (reply keyboard), replacing the previous
screen's buttons, and remembers "label -> action" for that user in the database.
When the user taps a key, Telegram sends its label as text; `nav_router` looks the
label up and runs the action exactly like an inline button press.

Inline buttons are still used on messages the bot sends on its own (e.g. the
admin's Approve / Reject under a subscription request).
"""
from __future__ import annotations

import contextvars
import html
import json
import re
from dataclasses import dataclass, field

from aiogram import BaseMiddleware, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           LinkPreviewOptions, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)

from .. import texts as T

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
KB_PRESS = "kb"             # id of a synthetic callback made from a keyboard-area key
_current: contextvars.ContextVar = contextvars.ContextVar("ctx", default=None)


@dataclass
class Ctx:
    cfg: object
    db: object
    lmap: object
    monitor: object
    dispatcher: object
    msg: object
    tasks: set = field(default_factory=set)


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([(text, action), ...], ...) - empty rows are skipped."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row])


def nav(back: str | None = None) -> list:
    """Navigation row. Shown only where it is useful: 'Geri' leads to the parent
    screen; 'Ana səhifə' is added only when Geri does not already lead home."""
    return ([(T.BTN_BACK, back)] if back else []) + [(T.BTN_HOME, "m:home")]


SHARE_PHONE = "contact:share"      # a key with this action asks Telegram for the user's own number


def home_markup(is_admin: bool, has_phone: bool = True) -> InlineKeyboardMarkup:
    return kb([(T.BTN_STATUS, "m:status"), (T.BTN_LINKS, "m:links")],
              [(T.BTN_SUBS, "m:subs"), (T.BTN_CONTACT, "m:contact")],
              [(T.BTN_HELP, "m:help")] + ([(T.BTN_ADMIN, "a:home")] if is_admin else []),
              [] if has_phone else [(T.BTN_PHONE, SHARE_PHONE)])


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def send(bot, uid: int, text: str, markup=None):
    """Send HTML text; if Telegram refuses the formatting, send it as plain text."""
    try:
        return await bot.send_message(uid, text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest:
        return await bot.send_message(uid, _plain(text)[:4096], reply_markup=markup, parse_mode=None,
                                      link_preview_options=NO_PREVIEW)


async def show_inline(event, text: str, markup: InlineKeyboardMarkup):
    """An extra message whose buttons stay UNDER it (lists of items to open)."""
    await send(_current.get().msg.bot, event.from_user.id, text, markup)


async def show(event, text: str, markup: InlineKeyboardMarkup | None = None):
    """Send a screen as a new message; its buttons replace the keyboard-area menu."""
    ctx, uid = _current.get(), event.from_user.id
    mapping, rows = {}, []
    for row in (markup.inline_keyboard if markup else []):
        keys = []
        for b in row:
            if b.text in mapping or (b.text == T.BTN_HOME and mapping.get(T.BTN_BACK) == "m:home"):
                continue                                 # 'Geri' already leads home
            if b.callback_data == SHARE_PHONE:           # Telegram sends the contact, not a text
                keys.append(KeyboardButton(text=b.text, request_contact=True))
                continue
            mapping[b.text] = b.callback_data
            keys.append(KeyboardButton(text=b.text))
        if keys:
            rows.append(keys)
    await ctx.db.kv_set(f"kbmap:{uid}", json.dumps(mapping, ensure_ascii=False))
    reply = (ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)
             if rows else ReplyKeyboardRemove())
    await send(ctx.msg.bot, uid, text, reply)
    if isinstance(event, CallbackQuery):
        await ack(event)


async def show_home(event, ctx, user, note: str = ""):
    uid = user["user_id"]
    await show(event, (note + "\n\n" if note else "") + T.main_menu(user),
               home_markup(ctx.cfg.is_admin(uid), bool(user["phone"])))


async def press(dispatcher, bot, from_user, data: str):
    """Run the action `data` exactly as if an inline button carrying it had been pressed."""
    await dispatcher.feed_update(bot, Update(update_id=0, callback_query=CallbackQuery(
        id=KB_PRESS, from_user=from_user, chat_instance=KB_PRESS, data=data)))


async def ack(c: CallbackQuery, text: str | None = None, alert: bool = False):
    """Answer a callback; for a keyboard-area press the text is sent as a message."""
    if c.id == KB_PRESS:
        ctx = _current.get()
        if text and text != "…" and ctx:
            await ctx.msg.user(c.from_user.id, text)
        return
    try:
        await c.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass


def action_name(data: str) -> str:
    """'l:v:15' -> 'l:v' : the action without its ids, for usage statistics."""
    return ":".join(p for p in (data or "").split(":")[:2] if not re.fullmatch(r"-?\d+", p))


class RegisterMiddleware(BaseMiddleware):
    """Every update: make sure the sender exists in the database (ID, username, first
    name, registration date), hand the row to the handler, count the action, and drop
    any half-finished text dialogue when a button is pressed."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        ctx: Ctx = data["ctx"]
        if user is None or user.is_bot:
            return None
        if isinstance(event, (Message, CallbackQuery)):
            chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
            synthetic = isinstance(event, CallbackQuery) and event.id == KB_PRESS
            if not synthetic and (chat is None or chat.type != "private"):
                return None
        _current.set(ctx)
        row, created = await ctx.db.upsert_user(user.id, user.username, user.first_name)
        if isinstance(event, CallbackQuery):
            await ctx.db.track(user.id, action_name(event.data))
            if row["state"]:
                await ctx.db.set_state(user.id, None)
                row = await ctx.db.get_user(user.id)
        data["user"], data["created"] = row, created
        return await handler(event, data)


class KeyboardKey(BaseFilter):
    """True when the text is a key of the menu currently shown to this user."""

    async def __call__(self, m: Message, ctx: Ctx):
        if not m.text or not m.from_user:
            return False
        mapping = json.loads(await ctx.db.kv_get(f"kbmap:{m.from_user.id}", "{}") or "{}")
        return {"target": mapping[m.text]} if m.text in mapping else False


nav_router = Router(name="nav")


@nav_router.message(KeyboardKey())
async def keyboard_key(m: Message, ctx: Ctx, user, dispatcher, bot, target: str):
    uid = user["user_id"]
    await ctx.db.set_state(uid, None)            # a menu key abandons any pending dialogue
    await ctx.db.kv_set(f"buf:{uid}", "")
    await press(dispatcher, bot, m.from_user, target)


@nav_router.message(Command("cancel"))
async def cancel(m: Message, ctx: Ctx, user):
    """Leave any pending dialogue (user or admin) and return to the main menu."""
    uid = user["user_id"]
    await ctx.db.set_state(uid, None)
    await ctx.db.kv_set(f"buf:{uid}", "")
    await show_home(m, ctx, await ctx.db.get_user(uid), T.CANCELLED)
