"""Команды приватности и очистка устаревших корзин/состояний."""

import asyncio
import logging
import sqlite3

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from . import texts
from .orders import OrdersRepo
from .panel import PanelClient, PanelError
from .storage import SqliteStorage

router = Router(name="privacy")
log = logging.getLogger(__name__)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(texts.PRIVACY)


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
            await panel_client.forget(user_id)
        except PanelError:
            log.warning("Panel privacy request failed")
            await message.answer(texts.FORGET_PANEL_FAILED)
            return
        await message.answer(texts.FORGET_DONE_PANEL)
    else:
        await message.answer(texts.FORGET_DONE)


class CleanupWorker:
    def __init__(self, storage: SqliteStorage, interval: float = 3600) -> None:
        self.storage = storage
        self.interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.storage.cleanup_expired()
            except sqlite3.Error:
                log.exception("FSM cleanup failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
