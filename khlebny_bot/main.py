import asyncio
import functools
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import texts
from .checkout import router as checkout_router
from .config import Settings
from .handlers import router
from .orders import OrdersRepo
from .owner import notify_owners
from .owner import router as owner_router
from .panel import PanelClient
from .storage import SqliteStorage


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
    dp = Dispatcher(
        storage=storage,
        orders=orders,
        panel_client=panel_client,
        owner_ids=owner_ids,
        owner_notifier=owner_notifier,
    )
    # Порядок важен: общие команды и кнопки меню срабатывают раньше шагов оформления.
    dp.include_router(router)
    dp.include_router(owner_router)
    dp.include_router(checkout_router)
    await bot.set_my_commands(
        [
            BotCommand(command="menu", description=texts.CMD_MENU),
            BotCommand(command="cart", description=texts.CMD_CART),
            BotCommand(command="cancel", description=texts.CMD_CANCEL),
            BotCommand(command="help", description=texts.CMD_HELP),
        ]
    )
    try:
        await dp.start_polling(bot)
    finally:
        if panel_client is not None:
            await panel_client.aclose()
        await orders.close()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
