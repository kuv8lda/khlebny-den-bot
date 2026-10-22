"""Опрос событий панели. Один последовательный воркер на базу заказов."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from . import texts
from .orders import OrdersRepo
from .panel import PanelClient, PanelError

log = logging.getLogger(__name__)


class EventsWorker:
    def __init__(
        self, bot: Bot, panel: PanelClient, orders: OrdersRepo, interval: float = 5
    ) -> None:
        self.bot = bot
        self.panel = panel
        self.orders = orders
        self.interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        cursor = await self.orders.event_cursor()
        events = await self.panel.fetch_events(cursor, limit=50)
        for event in events:
            if self._stop.is_set():
                break
            if event.id <= cursor:
                continue
            order = await self.orders.get_by_panel_id(event.order_id)
            status = texts.EVENT_STATUS_NAMES.get(event.status)
            if order is None or status is None:
                # История панели может содержать заказы другого/старого экземпляра бота.
                log.warning("Skipping panel event %s: unknown order or status", event.id)
                await self.orders.ack_event(event.id)
            else:
                try:
                    await self.bot.send_message(
                        chat_id=order.user_id,
                        text=texts.ORDER_STATUS_NOTIFICATION.format(id=order.id, status=status),
                    )
                except TelegramForbiddenError:
                    # Блокировка бота не должна останавливать уведомления всех клиентов.
                    log.warning("Cannot deliver panel event %s: bot forbidden", event.id)
                await self.orders.ack_event(event.id, order.id, event.status)
            cursor = event.id

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except (PanelError, TelegramAPIError) as exc:
                log.warning("Events polling failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass
