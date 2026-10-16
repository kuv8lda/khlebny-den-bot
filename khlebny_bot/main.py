import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import texts
from .checkout import router as checkout_router
from .config import Settings
from .handlers import router
from .orders import OrdersRepo
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
    dp = Dispatcher(storage=storage, orders=orders, panel_client=panel_client)
    # Порядок важен: общие команды и кнопки меню срабатывают раньше шагов оформления.
    dp.include_router(router)
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
