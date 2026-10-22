"""FSM-хранилище на SQLite (stdlib sqlite3), переживает перезапуск бота."""

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey


class SqliteStorage(BaseStorage):
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._keys = DefaultKeyBuilder(with_destiny=True)
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS fsm ("
                "key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}')"
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(fsm)")}
            if "updated_at" not in columns:
                self._conn.execute("ALTER TABLE fsm ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
                # Возраст старых записей неизвестен: дать им полный срок хранения.
                self._conn.execute("UPDATE fsm SET updated_at=?", (time.time(),))

    def _key(self, key: StorageKey) -> str:
        return self._keys.build(key, "data")

    def _run(self, sql: str, params: tuple = (), fetch: bool = False):
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return cur.fetchone() if fetch else None

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await asyncio.to_thread(
            self._run,
            "INSERT INTO fsm(key, state, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (self._key(key), value, time.time()),
        )

    async def get_state(self, key: StorageKey) -> str | None:
        row = await asyncio.to_thread(
            self._run, "SELECT state FROM fsm WHERE key=?", (self._key(key),), True
        )
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await asyncio.to_thread(
            self._run,
            "INSERT INTO fsm(key, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (self._key(key), json.dumps(dict(data), ensure_ascii=False), time.time()),
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._run, "SELECT data FROM fsm WHERE key=?", (self._key(key),), True
        )
        return json.loads(row[0]) if row else {}

    def _forget(self, user_id: int) -> None:
        with self._lock, self._conn:
            keys = self._conn.execute("SELECT key FROM fsm").fetchall()
            # DefaultKeyBuilder: fsm:chat_id[:thread_id]:user_id:destiny:data.
            self._conn.executemany(
                "DELETE FROM fsm WHERE key=?",
                [(key,) for (key,) in keys if key.split(":")[-3] == str(user_id)],
            )

    async def forget(self, user_id: int) -> None:
        """Удалить все состояния пользователя, включая другие чаты и темы."""
        await asyncio.to_thread(self._forget, user_id)

    async def cleanup_expired(self, max_age: float = 7 * 24 * 60 * 60) -> None:
        await asyncio.to_thread(
            self._run, "DELETE FROM fsm WHERE updated_at < ?", (time.time() - max_age,)
        )

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
