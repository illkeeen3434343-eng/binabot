"""Shared bot plumbing: context object, keyboards, screen rendering, registration."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           LinkPreviewOptions, Message, ReplyKeyboardMarkup, Update)

from .. import texts as T

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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
    """kb([(text, data), ...], ...) - empty rows are skipped."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row])


def nav(back: str | None = None) -> list:
    """The navigation row: Back -> previous logical menu, Home -> main menu."""
    return ([(T.BTN_BACK, back)] if back else []) + [(T.BTN_HOME, "m:home")]


KB_PRESS = "kb"             # id of a synthetic callback made from a keyboard-area button
_current: contextvars.ContextVar = contextvars.ContextVar("ctx", default=None)


def main_kb(is_admin: bool) -> ReplyKeyboardMarkup:
    """The permanent menu in the phone's keyboard area."""
    rows = [[T.BTN_STATUS, T.BTN_LINKS], [T.BTN_SUBS, T.BTN_HELP], [T.BTN_BACK, T.BTN_HOME]]
    if is_admin:
        rows.append([T.BTN_ADMIN])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
                               resize_keyboard=True, is_persistent=True)


KEYBOARD_ACTIONS = {T.BTN_STATUS: "m:status", T.BTN_LINKS: "m:links", T.BTN_SUBS: "m:subs",
                    T.BTN_HELP: "m:help", T.BTN_HOME: "m:home", T.BTN_ADMIN: "a:home", T.BTN_BACK: None}


async def press(dispatcher, bot, from_user, data: str):
    """Run the screen behind `data` exactly as if its inline button had been pressed."""
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


async def _remember_back(uid: int, markup):
    """Store where 'Geri' leads from the screen just shown (its inline Back button)."""
    ctx = _current.get()
    if not ctx:
        return
    target = "m:home"
    for row in getattr(markup, "inline_keyboard", None) or []:
        for b in row:
            if b.text == T.BTN_BACK and b.callback_data:
                target = b.callback_data
    await ctx.db.kv_set(f"back:{uid}", target)


async def show(event, text: str, markup=None):
    """Render a screen. Inline button -> edit that message in place. Keyboard-area button
    or typed text -> a new message at the bottom of the chat."""
    await _remember_back(event.from_user.id, markup)
    if isinstance(event, CallbackQuery):
        if event.message is None:                              # keyboard-area press
            ctx = _current.get()
            await ctx.msg.bot.send_message(event.from_user.id, text, reply_markup=markup,
                                           link_preview_options=NO_PREVIEW)
            return
        try:
            await event.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():       # e.g. message too old to edit
                await event.message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        await ack(event)
    else:
        await event.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def show_home(event, ctx, user):
    """Main menu: always a NEW message, because it carries the keyboard-area menu."""
    uid = user["user_id"]
    await ctx.db.kv_set(f"back:{uid}", "m:home")
    await ctx.msg.bot.send_message(uid, T.main_menu(user), reply_markup=main_kb(ctx.cfg.is_admin(uid)))
    if isinstance(event, CallbackQuery):
        await ack(event)


class RegisterMiddleware(BaseMiddleware):
    """Every update: make sure the sender exists in the database (ID, username, first
    name, registration date), hand the row to the handler, and drop any half-finished
    text dialogue when a button is pressed."""

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
        if isinstance(event, CallbackQuery) and row["state"]:
            await ctx.db.set_state(user.id, None)
            row = await ctx.db.get_user(user.id)
        data["user"], data["created"] = row, created
        return await handler(event, data)
