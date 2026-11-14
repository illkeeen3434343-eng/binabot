"""Outgoing Telegram messages that are not replies (notifications, alerts, ads)."""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter)
from aiogram.types import BufferedInputFile, LinkPreviewOptions

from ..bina.parser import Ad
from ..texts import ad_caption

log = logging.getLogger("messenger")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Messenger:
    def __init__(self, bot, cfg, db, client=None):
        self.bot, self.cfg, self.db, self.client = bot, cfg, db, client

    async def user(self, user_id: int, text: str, kb=None) -> bool:
        try:
            await self.bot.send_message(user_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
            return True
        except TelegramForbiddenError:
            await self.db.set_user(user_id, blocked_bot=1)
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 30))
            try:
                await self.bot.send_message(user_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
                return True
            except Exception:
                log.exception("message to %s failed after flood wait", user_id)
        except Exception:
            log.exception("message to %s failed", user_id)
        return False

    async def admins(self, text: str, kb=None):
        for aid in self.cfg.admin_ids:
            await self.user(aid, text, kb)

    async def send_ad(self, user_id: int, ad: Ad) -> str:
        """-> 'sent' | 'retry' (try again later) | 'blocked' (user stopped the bot).
        Photo by URL (no request from our IP) -> photo uploaded by us -> plain text.
        The advertisement is never lost because of its picture."""
        caption = ad_caption(ad)
        attempts = []
        if ad.photo:
            attempts.append(lambda: self.bot.send_photo(user_id, ad.photo, caption=caption))

            async def upload():
                data = await self.client.fetch_image(ad.photo) if self.client else None
                if not data:
                    raise TelegramBadRequest(method=None, message="no image bytes")
                return await self.bot.send_photo(user_id, BufferedInputFile(data, "elan.jpg"), caption=caption)
            attempts.append(upload)
        attempts.append(lambda: self.bot.send_message(user_id, caption, link_preview_options=NO_PREVIEW))
        for flood_retry in (False, True):
            for send in attempts:
                try:
                    await send()
                    return "sent"
                except TelegramBadRequest as e:          # bad/unreachable picture -> next fallback
                    log.info("ad %s to %s: %s - falling back", ad.id, user_id, e)
                    continue
                except TelegramForbiddenError:
                    return "blocked"
                except TelegramRetryAfter as e:
                    if flood_retry or e.retry_after > 60:
                        return "retry"
                    await asyncio.sleep(e.retry_after)
                    break                                # one more round after the wait
                except (TelegramNetworkError, asyncio.TimeoutError):
                    return "retry"
            else:
                return "retry"
        return "retry"
