"""Ограничение входящих сообщений и учёт активных обработчиков."""

import asyncio
import time
from collections import OrderedDict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Update

from . import texts


class TrafficMiddleware(BaseMiddleware):
    def __init__(self, interval: float = 0.5, *, clock=time.monotonic) -> None:
        self.interval = interval
        self.clock = clock
        self.seen: OrderedDict[int, float] = OrderedDict()
        self.active: set[asyncio.Task] = set()
        self.stopping = False

    async def __call__(self, handler, event, data):
        if self.stopping:
            return None
        incoming = event.event if isinstance(event, Update) else event
        user = data.get("event_from_user")
        if user is not None:
            now = self.clock()
            while self.seen and next(iter(self.seen.values())) <= now - self.interval:
                self.seen.popitem(last=False)
            if user.id in self.seen:
                if isinstance(incoming, CallbackQuery):
                    await incoming.answer(texts.MESSAGE_THROTTLED)
                return None
            self.seen[user.id] = now
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await handler(event, data)
        finally:
            self.active.discard(task)

    async def drain(self, **kwargs) -> None:
        self.stopping = True
        if self.active:
            await asyncio.gather(*tuple(self.active), return_exceptions=True)
