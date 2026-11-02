"""Цены конкурентов из панели: отдельный воркер, не связанный с заказами."""

import asyncio
import logging
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from . import texts
from .cart import format_rub
from .panel import CompetitorPrice, PanelClient, PanelError

log = logging.getLogger(__name__)
router = Router()


class CompetitorRepo:
    """Последняя обработанная цена отдельно для каждого получателя."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS competitor_prices ("
            "owner_id INTEGER NOT NULL, competitor_id INTEGER NOT NULL, "
            "product_id INTEGER NOT NULL, price_cents INTEGER NOT NULL, "
            "PRIMARY KEY (owner_id, competitor_id, product_id))"
        )
        self._conn.commit()

    async def previous(self, owner_id: int, price: CompetitorPrice) -> int | None:
        def read():
            with self._lock:
                row = self._conn.execute(
                    "SELECT price_cents FROM competitor_prices "
                    "WHERE owner_id=? AND competitor_id=? AND product_id=?",
                    (owner_id, price.competitor_id, price.product_id),
                ).fetchone()
                return row[0] if row else None

        return await asyncio.to_thread(read)

    async def save(self, owner_id: int, price: CompetitorPrice) -> None:
        def write():
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO competitor_prices VALUES (?, ?, ?, ?)",
                    (owner_id, price.competitor_id, price.product_id, price.price_cents),
                )

        await asyncio.to_thread(write)

    async def close(self) -> None:
        def close():
            with self._lock:
                self._conn.close()

        await asyncio.to_thread(close)


def significant(old: int, new: int) -> bool:
    # Для нулевой исходной цены процент не определён.
    return old > 0 and abs(new - old) * 100 > old * 5


def notification(price: CompetitorPrice, old: int) -> str:
    percent = Decimal(abs(price.price_cents - old)) * 100 / Decimal(old)
    change = "подешевел" if price.price_cents < old else "подорожал"
    own = format_rub(price.own_price_cents) if price.own_price_cents is not None else "неизвестна"
    return (
        f"Конкурент {price.competitor_name}: {price.product_name} {change} "
        f"на {percent:.2f}% (было {format_rub(old)}, "
        f"стало {format_rub(price.price_cents)}). Ваша цена: {own}"
    )


class CompetitorWorker:
    def __init__(
        self, bot: Bot, panel: PanelClient, repo: CompetitorRepo,
        owner_ids: frozenset[int], interval: float = 5,
    ) -> None:
        self.bot = bot
        self.panel = panel
        self.repo = repo
        self.owner_ids = owner_ids
        self.interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        prices = await self.panel.fetch_competitor_prices()
        for price in prices:
            for owner_id in sorted(self.owner_ids):
                if self._stop.is_set():
                    return
                old = await self.repo.previous(owner_id, price)
                if old is not None and significant(old, price.price_cents):
                    try:
                        await self.bot.send_message(
                            chat_id=owner_id, text=notification(price, old), parse_mode=None,
                        )
                    except TelegramForbiddenError:
                        log.warning("Competitor notification forbidden for owner %s", owner_id)
                    except TelegramAPIError:
                        # Другие владельцы продолжают получать сообщения; этому повторим позже.
                        log.warning("Competitor notification failed for owner %s", owner_id)
                        continue
                await self.repo.save(owner_id, price)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except PanelError:
                log.warning("Competitor prices polling failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass


@router.message(Command("competitor"))
async def cmd_competitor(
    message: Message, panel_client: PanelClient | None = None,
    owner_ids: frozenset[int] = frozenset(),
) -> None:
    if message.from_user is None or message.from_user.id not in owner_ids:
        await message.answer(texts.NO_ACCESS)
        return
    if panel_client is None:
        await message.answer("Цены конкурентов недоступны: подключите панель.")
        return
    try:
        prices = await panel_client.fetch_competitor_prices()
    except PanelError:
        await message.answer("Не удалось получить цены конкурентов. Попробуйте позже.")
        return
    if not prices:
        await message.answer("Пока нет данных о ценах конкурентов.")
        return
    # Отдельные сообщения не превышают лимит Telegram даже при большом каталоге.
    lines = ["Последние известные цены конкурентов:"]
    for price in prices:
        own = (
            format_rub(price.own_price_cents)
            if price.own_price_cents is not None else "неизвестна"
        )
        line = (
            f"Конкурент {price.competitor_name}: {price.product_name} — "
            f"{format_rub(price.price_cents)}. Ваша цена: {own}"
        )
        if len("\n".join([*lines, line])) > 3500:
            await message.answer("\n".join(lines), parse_mode=None)
            lines = []
        lines.append(line)
    if lines:
        await message.answer("\n".join(lines), parse_mode=None)
