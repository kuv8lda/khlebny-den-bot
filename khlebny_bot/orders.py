"""Репозиторий заказов на SQLite (stdlib sqlite3 через asyncio.to_thread).

Название и цена позиции фиксируются в order_items на момент заказа.
Повторное создание с тем же checkout_token возвращает уже созданный заказ.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUS_NEW = "new"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    checkout_token TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id);
CREATE TABLE IF NOT EXISTS order_items (
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    qty INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
"""

_COLS = "id, user_id, name, comment, status, total_cents, created_at"


@dataclass(frozen=True)
class OrderItem:
    item_id: int
    name: str
    price_cents: int
    qty: int


@dataclass(frozen=True)
class Order:
    id: int
    user_id: int
    name: str
    comment: str
    status: str
    total_cents: int
    created_at: str  # ISO 8601, UTC
    items: tuple[OrderItem, ...] = ()


class OrdersRepo:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)

    def _items(self, order_id: int) -> tuple[OrderItem, ...]:
        rows = self._conn.execute(
            "SELECT item_id, name, price_cents, qty FROM order_items "
            "WHERE order_id=? ORDER BY rowid",
            (order_id,),
        ).fetchall()
        return tuple(OrderItem(*row) for row in rows)

    def _create(
        self, user_id: int, name: str, comment: str, items: Sequence[OrderItem], token: str
    ) -> tuple[Order, bool]:
        if not items:
            raise ValueError("order must contain at least one item")
        with self._lock, self._conn:  # одна транзакция
            row = self._conn.execute(
                f"SELECT {_COLS} FROM orders WHERE checkout_token=?", (token,)
            ).fetchone()
            if row is not None:
                return Order(*row, items=self._items(row[0])), False
            total = sum(i.price_cents * i.qty for i in items)
            created_at = datetime.now(UTC).isoformat(timespec="seconds")
            cur = self._conn.execute(
                "INSERT INTO orders(user_id, name, comment, status, total_cents, created_at, "
                "checkout_token) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (user_id, name, comment, STATUS_NEW, total, created_at, token),
            )
            order_id = cur.lastrowid
            assert order_id is not None
            self._conn.executemany(
                "INSERT INTO order_items(order_id, item_id, name, price_cents, qty) "
                "VALUES(?, ?, ?, ?, ?)",
                [(order_id, i.item_id, i.name, i.price_cents, i.qty) for i in items],
            )
            order = Order(
                order_id, user_id, name, comment, STATUS_NEW, total, created_at, tuple(items)
            )
            return order, True

    def _list(self, user_id: int, limit: int) -> list[Order]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [Order(*row) for row in rows]

    def _get(self, order_id: int, user_id: int) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLS} FROM orders WHERE id=? AND user_id=?", (order_id, user_id)
            ).fetchone()
            if row is None:
                return None
            return Order(*row, items=self._items(row[0]))

    async def create(
        self, user_id: int, name: str, comment: str, items: Sequence[OrderItem], token: str
    ) -> tuple[Order, bool]:
        """Вернуть (заказ, создан_ли_сейчас). Тот же token даёт тот же заказ."""
        return await asyncio.to_thread(self._create, user_id, name, comment, items, token)

    async def list_for_user(self, user_id: int, limit: int = 5) -> list[Order]:
        """Последние заказы пользователя, новые первыми (без состава)."""
        return await asyncio.to_thread(self._list, user_id, limit)

    async def get(self, order_id: int, user_id: int) -> Order | None:
        """Заказ с составом; чужой заказ не отдаётся."""
        return await asyncio.to_thread(self._get, order_id, user_id)

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
