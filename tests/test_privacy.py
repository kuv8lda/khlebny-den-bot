import asyncio
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from helpers import make_message, make_state

from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError
from khlebny_bot.privacy import CleanupWorker, cmd_forget, cmd_privacy, render_privacy
from khlebny_bot.storage import SqliteStorage


async def test_privacy():
    message = make_message()
    await cmd_privacy(message)
    text, markup = render_privacy("privacy")
    message.answer.assert_awaited_once_with(text, reply_markup=markup)


@pytest.mark.parametrize("panel_status", [None, "ok", "failed"])
async def test_forget(tmp_path, panel_status):
    storage = SqliteStorage(tmp_path / "state.db")
    orders = OrdersRepo(tmp_path / "orders.db")
    state = make_state(storage)
    other_chat = FSMContext(storage, StorageKey(bot_id=1, chat_id=-100, user_id=1))
    other_user = make_state(storage, user_id=2)
    try:
        for context in (state, other_chat, other_user):
            await context.set_state("checkout:name")
            await context.set_data({"cart": {"1": 2}, "name": "Имя"})
        item = OrderItem(1, "Хлеб", 100, 1)
        own, _ = await orders.create(1, "Имя", "Секрет", [item], "one")
        foreign, _ = await orders.create(2, "Другой", "", [item], "two")
        await orders.ack_event(15)
        panel = AsyncMock(spec=PanelClient) if panel_status else None
        if panel_status == "failed":
            panel.forget.side_effect = PanelError("offline")
        message = make_message()
        await cmd_forget(message, state, orders, panel)
        assert await state.get_data() == {}
        assert await state.get_state() is None
        assert await other_chat.get_data() == {}
        assert await other_user.get_state() == "checkout:name"
        assert await orders.get_by_id(own.id) is None
        assert await orders.get_by_id(foreign.id) is not None
        assert await orders.event_cursor() == 15
        with sqlite3.connect(tmp_path / "orders.db") as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM order_items WHERE order_id=?", (own.id,)
                ).fetchone()[0]
                == 0
            )
        document = {None: "local", "ok": "panel", "failed": "failed"}[panel_status]
        expected, markup = render_privacy(document)
        message.answer.assert_awaited_once_with(expected, reply_markup=markup)
        if panel is not None:
            panel.forget.assert_awaited_once_with(1)
        await cmd_forget(message, state, orders, panel)
    finally:
        await orders.close()
        await storage.close()


async def test_cleanup_age_and_writes_refresh_timestamp(tmp_path, monkeypatch):
    now = 1_000_000
    monkeypatch.setattr("khlebny_bot.storage.time.time", lambda: now)
    storage = SqliteStorage(tmp_path / "state.db")
    old = make_state(storage)
    active = make_state(storage, user_id=2)
    try:
        for state in (old, active):
            await state.set_state("checkout:name")
            await state.set_data({"cart": {"1": 1}})
        now += 7 * 86400
        await storage.cleanup_expired()
        assert await old.get_state() is not None  # ровно 7 дней — ещё не старше
        await active.update_data(name="Новое имя")
        now += 1
        await storage.cleanup_expired()
        assert await old.get_state() is None
        assert await old.get_data() == {}
        assert (await active.get_data())["name"] == "Новое имя"
        assert await active.get_state() is not None
    finally:
        await storage.close()


async def test_migrate_old_fsm(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE fsm(key TEXT PRIMARY KEY, state TEXT, data TEXT)")
        conn.execute("INSERT INTO fsm VALUES('fsm:1:1:default:data', 'step', '{}')")
    monkeypatch.setattr("khlebny_bot.storage.time.time", lambda: 1_000_000)
    storage = SqliteStorage(path)
    await storage.cleanup_expired()
    assert await make_state(storage).get_state() == "step"
    await storage.close()
    storage = SqliteStorage(path)
    assert await make_state(storage).get_state() == "step"
    await storage.close()


async def test_cleanup_worker_runs_and_stops():
    storage = AsyncMock(spec=SqliteStorage)
    worker = CleanupWorker(storage)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)
    worker.stop()
    await asyncio.wait_for(task, 1)
    storage.cleanup_expired.assert_awaited_once_with()


@pytest.mark.parametrize("status", [200, 401, 422, 500])
@respx.mock
async def test_panel_forget(status):
    route = respx.post("https://panel.test/api/integrations/forget").mock(
        return_value=httpx.Response(status, json={"ok": True, "anonymized": 1})
    )
    client = PanelClient("https://panel.test", "key")
    try:
        if status == 200:
            await client.forget(123)
        else:
            with pytest.raises(PanelError):
                await client.forget(123)
        assert route.calls[0].request.content == b'{"telegram_user_id":123}'
        assert route.calls[0].request.headers["Authorization"] == "Bearer key"
    finally:
        await client.aclose()


@pytest.mark.parametrize("failure", ["network", "invalid", "false"])
@respx.mock
async def test_panel_forget_failed_confirmation(failure):
    route = respx.post("https://panel.test/api/integrations/forget")
    if failure == "network":
        route.mock(side_effect=httpx.ConnectError("offline"))
    else:
        route.mock(
            return_value=httpx.Response(
                200, text="not json" if failure == "invalid" else '{"ok": false}'
            )
        )
    client = PanelClient("https://panel.test", "key")
    try:
        with pytest.raises(PanelError):
            await client.forget(1)
    finally:
        await client.aclose()
