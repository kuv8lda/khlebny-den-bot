"""FSM-хранилище на SQLite (stdlib sqlite3), переживает перезапуск бота."""

import asyncio
import json
import sqlite3
import threading
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
            "INSERT INTO fsm(key, state) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state",
            (self._key(key), value),
        )

    async def get_state(self, key: StorageKey) -> str | None:
        row = await asyncio.to_thread(
            self._run, "SELECT state FROM fsm WHERE key=?", (self._key(key),), True
        )
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await asyncio.to_thread(
            self._run,
            "INSERT INTO fsm(key, data) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (self._key(key), json.dumps(dict(data), ensure_ascii=False)),
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._run, "SELECT data FROM fsm WHERE key=?", (self._key(key),), True
        )
        return json.loads(row[0]) if row else {}

    async def close(self) -> None:
        with self._lock:
            self._conn.close()
