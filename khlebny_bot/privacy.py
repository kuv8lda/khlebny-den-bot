"""Команды приватности и очистка устаревших корзин/состояний."""

import asyncio
import logging
import re
import sqlite3

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from . import texts
from .orders import OrdersRepo
from .panel import PanelClient, PanelError
from .storage import SqliteStorage
from .ui import one_line, page_slice

router = Router(name="privacy")
log = logging.getLogger(__name__)


class PrivacyPageCb(CallbackData, prefix="pp"):
    document: str
    page: int = 0


def _documents() -> dict[str, tuple[str, str]]:
    return {
        "privacy": ("🔒 О ваших данных", texts.PRIVACY),
        "local": ("✓ Данные бота удалены", texts.FORGET_DONE),
        "panel": ("✓ Данные бота удалены", texts.FORGET_DONE_PANEL),
        "failed": ("⚠️ Панель не подтвердила анонимизацию", texts.FORGET_PANEL_FAILED),
    }


def render_privacy(document: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    title, body = _documents()[document]
    sentences = re.split(r"(?<=[.!?])\s+", one_line(body))
    visible, page, pages = page_slice(sentences, page, 3)
    text = "\n".join([f"{title} · {page + 1}/{pages}", *visible])
    nav = [
        InlineKeyboardButton(
            text=label, callback_data=PrivacyPageCb(document=document, page=n).pack()
        )
        for label, n in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1))
        if 0 <= n < pages
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    return text, markup


async def _send_document(message: Message, document: str) -> None:
    text, markup = render_privacy(document)
    await message.answer(text, reply_markup=markup)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await _send_document(message, "privacy")


@router.callback_query(PrivacyPageCb.filter())
async def on_privacy_page(callback: CallbackQuery, callback_data: PrivacyPageCb) -> None:
    if callback_data.document not in _documents():
        await callback.answer("Раздел не найден. Откройте /privacy.", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        text, markup = render_privacy(callback_data.document, callback_data.page)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise


@router.message(Command("forget"))
async def cmd_forget(
    message: Message,
    state: FSMContext,
    orders: OrdersRepo,
    panel_client: PanelClient | None = None,
) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if isinstance(state.storage, SqliteStorage):
        await state.storage.forget(user_id)
    else:
        await state.clear()
    await orders.forget(user_id)
    if panel_client is not None:
        try:
            async with ChatActionSender.typing(
                bot=message.bot,
                chat_id=message.chat.id,
                message_thread_id=message.message_thread_id,
            ):
                await panel_client.forget(user_id)
        except PanelError:
            log.warning("Panel privacy request failed")
            await _send_document(message, "failed")
            return
        await _send_document(message, "panel")
    else:
        await _send_document(message, "local")


class CleanupWorker:
    def __init__(
        self, storage: SqliteStorage, interval: float = 3600, *, orders: OrdersRepo | None = None
    ) -> None:
        self.orders = orders
        self.storage = storage
        self.interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.storage.cleanup_expired()
                if self.orders is not None:
                    await self.orders.cleanup_rate_limits()
            except sqlite3.Error:
                log.exception("FSM cleanup failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
