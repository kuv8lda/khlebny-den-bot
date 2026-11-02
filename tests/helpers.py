"""Общие заглушки для тестов хендлеров."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message


def make_state(storage=None, user_id: int = 1) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage or MemoryStorage(), key=key)


def make_message(text: str | None = None, user_id: int = 1) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.bot = SimpleNamespace(id=1, send_chat_action=AsyncMock())
    msg.text = text
    msg.chat = SimpleNamespace(id=user_id)
    msg.message_thread_id = None
    msg.from_user = SimpleNamespace(id=user_id)
    return msg


def make_callback(user_id: int = 1) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = None
    cb.bot = SimpleNamespace(id=1, send_chat_action=AsyncMock())
    cb.answer = AsyncMock()
    cb.message = make_message(user_id=user_id)
    cb.from_user = SimpleNamespace(id=user_id)
    return cb


async def make_checkout_callback(state, action: str, user_id: int = 1):
    from khlebny_bot.handlers import CheckoutCb

    cb = make_callback(user_id)
    token = (await state.get_data())["checkout"]["token"]
    cb.data = CheckoutCb(action=action, token=token).pack()
    return cb
