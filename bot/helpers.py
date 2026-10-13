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
    msg.text = text
    msg.from_user = SimpleNamespace(id=user_id)
    return msg


def make_callback(user_id: int = 1) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    cb.message = make_message(user_id=user_id)
    cb.from_user = SimpleNamespace(id=user_id)
    return cb
