import asyncio
import functools
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand
from aiohttp import web

from . import texts
from .catalog import CATALOG
from .catalog_push import create_catalog_app
from .checkout import router as checkout_router
from .competitor import CompetitorRepo, CompetitorWorker
from .competitor import router as competitor_router
from .config import Settings
from .events import EventsWorker
from .handlers import router
from .orders import OrdersRepo
from .owner import notify_owners
from .owner import router as owner_router
from .panel import PanelClient, PanelError
from .privacy import CleanupWorker
from .privacy import router as privacy_router
from .reliability import TrafficMiddleware
from .storage import SqliteStorage


async def wait_for_telegram(bot: Bot) -> None:
    """Keep startup alive while Telegram is temporarily unreachable."""
    delay = 1
    while True:
        try:
            await bot.get_me(request_timeout=10)
            return
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            wait = exc.retry_after if isinstance(exc, TelegramRetryAfter) else delay
            logging.warning(
                "Telegram недоступен (%s). Повтор подключения через %s с.",
                type(exc).__name__,
                wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)


async def set_commands_with_retry(bot: Bot) -> None:
    """Configure Telegram commands without making a transient outage fatal."""
    commands = [
        BotCommand(command="menu", description=texts.CMD_MENU),
        BotCommand(command="cart", description=texts.CMD_CART),
        BotCommand(command="orders", description=texts.BTN_ORDERS),
        BotCommand(command="cancel", description=texts.CMD_CANCEL),
        BotCommand(command="help", description=texts.CMD_HELP),
        BotCommand(command="privacy", description=texts.CMD_PRIVACY),
        BotCommand(command="forget", description=texts.CMD_FORGET),
    ]
    delay = 1
    while True:
        try:
            await bot.set_my_commands(commands, request_timeout=10)
            return
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            wait = exc.retry_after if isinstance(exc, TelegramRetryAfter) else delay
            logging.warning(
                "Не удалось настроить команды Telegram (%s). Повтор через %s с.",
                type(exc).__name__,
                wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    storage = SqliteStorage(settings.fsm_db_path)
    orders = OrdersRepo(settings.orders_path)
    panel_client: PanelClient | None = None
    if settings.panel_enabled:
        panel_client = PanelClient(
            settings.panel_base_url,
            settings.panel_api_key.get_secret_value().strip(),
            settings.panel_timeout_seconds,
        )
    bot = Bot(settings.bot_token.get_secret_value())
    owner_ids = settings.owner_ids
    if owner_ids and panel_client is None:
        logging.warning("OWNER_TELEGRAM_IDS задан, но PANEL_API_KEY пуст: кнопок статусов не будет")
    owner_notifier = functools.partial(notify_owners, bot, owner_ids) if owner_ids else None
    isolation = SimpleEventIsolation()
    dp = Dispatcher(
        storage=storage,
        events_isolation=isolation,
        orders=orders,
        panel_client=panel_client,
        owner_ids=owner_ids,
        owner_notifier=owner_notifier,
    )
    traffic = TrafficMiddleware()
    # Учёт задач до FSM-lock: shutdown ждёт также обработчики, ожидающие свой lock.
    dp.update.outer_middleware.unregister(dp.fsm)
    dp.update.outer_middleware(traffic)
    dp.update.outer_middleware(dp.fsm)
    dp.shutdown.handlers = [h for h in dp.shutdown.handlers if h.callback != dp.fsm.close]
    dp.shutdown.register(traffic.drain)
    # Порядок важен: общие команды и кнопки меню срабатывают раньше шагов оформления.
    dp.include_router(router)
    dp.include_router(competitor_router)
    dp.include_router(owner_router)
    dp.include_router(privacy_router)
    dp.include_router(checkout_router)
    worker = (
        EventsWorker(bot, panel_client, orders, settings.events_poll_interval)
        if panel_client is not None
        else None
    )
    competitor_repo = None
    competitor_worker = None
    competitor_task = None
    if panel_client is not None and owner_ids:
        competitor_repo = CompetitorRepo(settings.orders_path)
        competitor_worker = CompetitorWorker(
            bot,
            panel_client,
            competitor_repo,
            owner_ids,
            settings.events_poll_interval,
        )
    cleanup = CleanupWorker(storage, orders=orders)
    cleanup_task = None
    worker_task = None
    catalog_runner = None
    try:
        push_token = settings.catalog_push_token.get_secret_value()
        saved_catalog = await storage.load_catalog() if push_token else None
        if saved_catalog is not None:
            CATALOG[:] = saved_catalog
        if panel_client is not None:
            try:
                if push_token:
                    catalog, version = await panel_client.fetch_catalog_versioned()
                    if await storage.sync_catalog_version(catalog, version):
                        CATALOG[:] = catalog
                else:
                    CATALOG[:] = await panel_client.fetch_catalog()
            except PanelError:
                if saved_catalog is None:
                    saved_catalog = await storage.load_catalog()
                if saved_catalog is None:
                    logging.warning("panel unavailable at startup, using fallback catalog")
                else:
                    CATALOG[:] = saved_catalog
                    logging.warning("panel unavailable at startup, using last saved catalog")
        await storage.sync_catalog(CATALOG)
        if push_token:
            catalog_runner = web.AppRunner(create_catalog_app(storage, push_token), access_log=None)
            await catalog_runner.setup()
            await web.TCPSite(
                catalog_runner, settings.catalog_push_host, settings.catalog_push_port
            ).start()
        await set_commands_with_retry(bot)
        cleanup_task = asyncio.create_task(cleanup.run(), name="privacy-cleanup")
        if worker is not None:
            worker_task = asyncio.create_task(worker.run(), name="panel-events")
        if competitor_worker is not None:
            competitor_task = asyncio.create_task(competitor_worker.run(), name="competitor-prices")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        try:
            if catalog_runner is not None:
                await catalog_runner.cleanup()
            await traffic.drain()
            cleanup.stop()
            if cleanup_task is not None:
                await cleanup_task
            if competitor_worker is not None:
                competitor_worker.stop()
                if competitor_task is not None:
                    await competitor_task
            if worker is not None and worker_task is not None:
                worker.stop()
                await worker_task
        finally:
            if competitor_repo is not None:
                await competitor_repo.close()
            if panel_client is not None:
                await panel_client.aclose()
            await orders.close()
            await isolation.close()
            await storage.close()
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
