import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import texts
from .config import Settings
from .handlers import router
from .storage import SqliteStorage


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    storage = SqliteStorage(settings.fsm_db_path)
    bot = Bot(settings.bot_token.get_secret_value())
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
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
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
