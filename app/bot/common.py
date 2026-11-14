"""Shared bot plumbing: context object, keyboards, screen rendering, registration."""
from __future__ import annotations

from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LinkPreviewOptions, Message)

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


def main_kb(is_admin: bool) -> InlineKeyboardMarkup:
    return kb([(T.BTN_STATUS, "m:status"), (T.BTN_LINKS, "m:links")],
              [(T.BTN_SUBS, "m:subs"), (T.BTN_HELP, "m:help")],
              [(T.BTN_ADMIN, "a:home")] if is_admin else [])


async def show(event, text: str, markup=None):
    """Render a screen: edit the message under the pressed button, or answer a text."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():       # e.g. message too old to edit
                await event.message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
    else:
        await event.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


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
            if chat is None or chat.type != "private":
                return None
        row, created = await ctx.db.upsert_user(user.id, user.username, user.first_name)
        if isinstance(event, CallbackQuery) and row["state"]:
            await ctx.db.set_state(user.id, None)
            row = await ctx.db.get_user(user.id)
        data["user"], data["created"] = row, created
        return await handler(event, data)
