"""Entry point: wires everything together and runs the bot + background loops."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher as AioDispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent

from .bina.client import BinaClient, Http
from .bina.links import LinkMap
from .bot import admin, user
from .bot.common import Ctx, RegisterMiddleware, nav_router
from .config import load_config
from .db import Database
from .services.alerts import Alerts
from .services.dispatcher import Dispatcher
from .services.jobs import Jobs
from .services.messenger import Messenger
from .services.monitor import Monitor

log = logging.getLogger("main")


def setup_logging(log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file = RotatingFileHandler(os.path.join(log_dir, "bot.log"), maxBytes=2_000_000, backupCount=7,
                               encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file, console])
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def build_dispatcher(ctx: Ctx, alerts: Alerts | None = None) -> AioDispatcher:
    dp = AioDispatcher()
    dp["ctx"] = ctx
    mw = RegisterMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(nav_router)            # keyboard-area keys -> actions
    dp.include_router(admin.router)          # admin first: its filter lets non-admins fall through
    dp.include_router(user.router)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        log.exception("handler error", exc_info=event.exception)
        if alerts:
            await alerts.error("bot", event.exception.__class__.__name__, str(event.exception))
        return True
    return dp


async def loop(name: str, fn, interval: float, alerts: Alerts, first_delay: float = 0):
    """Run fn forever; a crash is reported to the admins and the loop continues."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("%s loop error", name)
            await alerts.error(name, e.__class__.__name__, str(e))
        await asyncio.sleep(interval)


async def main():
    cfg = load_config()
    setup_logging(cfg.log_dir)
    if not cfg.bot_token or not cfg.admin_ids:
        sys.exit("BOT_TOKEN and ADMIN_IDS must be set (see .env.example)")
    db = await Database(cfg.db_path).open()
    session = None
    if cfg.telegram_api_base:                      # self-hosted Bot API server (optional)
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
        session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.telegram_api_base))
    bot = Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    http = Http(cfg.request_timeout)
    client = BinaClient(http, cfg.persisted_hash, cfg.request_min_interval, cfg.request_retries)
    msg = Messenger(bot, cfg, db, client)
    alerts = Alerts(db, cfg, msg.admins)
    monitor = Monitor(db, cfg, client, alerts)
    dispatcher = Dispatcher(db, cfg, msg, client)
    jobs = Jobs(db, cfg, msg, monitor)
    ctx = Ctx(cfg=cfg, db=db, lmap=LinkMap.load(cfg.map_path), monitor=monitor, dispatcher=dispatcher, msg=msg)
    dp = build_dispatcher(ctx, alerts)

    await bot.set_my_commands([BotCommand(command="start", description="Ana səhifə"),
                               BotCommand(command="cancel", description="Əməliyyatı ləğv et")])
    for aid in cfg.admin_ids:
        try:
            await bot.set_my_commands([BotCommand(command="start", description="Ana səhifə"),
                                       BotCommand(command="cancel", description="Əməliyyatı ləğv et"),
                                       BotCommand(command="admin", description="Admin panel")],
                                      scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            log.warning("admin %s has not started the bot yet", aid)

    tasks = [asyncio.create_task(loop("monitor", monitor.run_cycle, cfg.check_interval, alerts,
                                      cfg.check_first_delay)),
             asyncio.create_task(loop("dispatcher", dispatcher.dispatch_once, cfg.dispatch_interval, alerts)),
             asyncio.create_task(loop("jobs", jobs.tick, 60, alerts))]
    log.info("bot started; admins=%s db=%s", sorted(cfg.admin_ids), cfg.db_path)
    try:
        # one update at a time: keeps the parts of a split long link in order
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"], handle_as_tasks=False)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await http.close()
        await bot.session.close()
        await db.close()
        log.info("bot stopped")
