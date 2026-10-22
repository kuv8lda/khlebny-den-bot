import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from helpers import make_callback, make_message, make_state

from khlebny_bot import checkout, texts
from khlebny_bot.orders import OrderItem, OrderRateLimitError, OrdersRepo
from khlebny_bot.panel import OrderPayload, PanelClient, PanelError, PanelOrderResult
from khlebny_bot.reliability import TrafficMiddleware


@respx.mock
async def test_retries_keep_key_and_backoff(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("khlebny_bot.panel.asyncio.sleep", sleep)
    route = respx.post("http://panel/api/integrations/orders").mock(side_effect=[
        httpx.ReadTimeout("timeout"), httpx.Response(503), httpx.Response(200, json={"id": 4})
    ])
    client = PanelClient("http://panel", "secret")
    try:
        result = await client.create_order(
            OrderPayload(customer_name="Аня", items=[], telegram_user_id=1),
            idempotency_key="stable-token",
        )
        assert result.panel_order_id == 4
        assert [call.request.headers["Idempotency-Key"] for call in route.calls] == [
            "stable-token"
        ] * 3
        assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5]
    finally:
        await client.aclose()


@respx.mock
async def test_validation_error_is_not_retried():
    route = respx.post("http://panel/api/integrations/orders").respond(422)
    client = PanelClient("http://panel", "secret")
    try:
        with pytest.raises(PanelError):
            await client.create_order(OrderPayload(customer_name="A", items=[], telegram_user_id=1))
        assert route.call_count == 1
    finally:
        await client.aclose()


async def test_checkout_failure_preserves_cart_and_retries_same_order(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.db")
    state = make_state()
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.side_effect = [PanelError("down"), PanelOrderResult(panel_order_id=9)]
    try:
        await state.update_data(cart={"1": 2})
        await checkout.on_checkout_start(make_callback(), state)
        await checkout.on_name(make_message("Аня"), state)
        await checkout.on_skip(make_callback(), state)
        saved = await state.get_data()
        await checkout.on_confirm(make_callback(), state, repo, panel)
        assert await state.get_data() == saved
        assert await state.get_state() == checkout.Checkout.confirm.state
        await checkout.on_confirm(make_callback(), state, repo, panel)
        assert len(await repo.list_for_user(1)) == 1
        assert [c.kwargs["idempotency_key"] for c in panel.create_order.await_args_list] == [
            saved["checkout"]["token"]
        ] * 2
        order = (await repo.list_for_user(1))[0]
        assert not order.pending_panel_sync
        assert order.panel_order_id == 9
        assert not (await state.get_data()).get("cart")
    finally:
        await repo.close()


async def test_order_limit_persists_and_duplicate_does_not_count(tmp_path):
    path = tmp_path / "orders.db"
    repo = OrdersRepo(path)
    items = [OrderItem(1, "Хлеб", 100, 1)]
    for n in range(5):
        await repo.create(1, "Аня", "", items, str(n), enforce_rate_limit=True)
    await repo.close()
    repo = OrdersRepo(path)
    try:
        _, created = await repo.create(1, "Аня", "", items, "0", enforce_rate_limit=True)
        assert not created
        with pytest.raises(OrderRateLimitError):
            await repo.create(1, "Аня", "", items, "sixth", enforce_rate_limit=True)
        await repo.create(2, "Аня", "", items, "other-user", enforce_rate_limit=True)
        repo._conn.execute("UPDATE order_rate_limits SET created_at='2000-01-01T00:00:00+00:00'")
        repo._conn.commit()
        await repo.create(1, "Аня", "", items, "new-hour", enforce_rate_limit=True)
    finally:
        await repo.close()


async def test_throttle_and_drain():
    clock = [0.0]
    middleware = TrafficMiddleware(clock=lambda: clock[0])
    data = {"event_from_user": SimpleNamespace(id=1)}
    handler = AsyncMock()
    cb = make_callback()
    await middleware(handler, cb, data)
    await middleware(handler, cb, data)
    assert handler.await_count == 1
    cb.answer.assert_awaited_once_with(texts.MESSAGE_THROTTLED)
    clock[0] = 1
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(event, data):
        started.set()
        await release.wait()

    task = asyncio.create_task(middleware(slow, cb, data))
    await started.wait()
    drain = asyncio.create_task(middleware.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    release.set()
    await task
    await drain
    await middleware(handler, cb, data)
    assert handler.await_count == 1


async def test_sigterm_drains_handlers_before_dispatcher_shutdown(monkeypatch):
    import signal
    from unittest.mock import MagicMock

    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    traffic = TrafficMiddleware()
    dp.shutdown.handlers = [h for h in dp.shutdown.handlers if h.callback != dp.fsm.close]
    dp.shutdown.register(traffic.drain)
    dp.shutdown.register(dp.fsm.close)
    started, release = asyncio.Event(), asyncio.Event()
    bot = MagicMock()
    bot.id = 1

    async def handler(event, data):
        started.set()
        await release.wait()
        await storage.set_data(make_state().key, {"saved": True})

    async def polling(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(dp, "_polling", polling)
    task = asyncio.create_task(traffic(handler, make_callback(), {}))
    await started.wait()
    polling_task = asyncio.create_task(dp.start_polling(bot, close_bot_session=False))
    await asyncio.sleep(0)
    dp._signal_stop_polling(signal.SIGTERM)
    await asyncio.sleep(0)
    assert not polling_task.done()
    release.set()
    await task
    await asyncio.wait_for(polling_task, 2)
    assert (await storage.get_data(make_state().key))["saved"]


async def test_counter_cleanup_preserves_current_hour(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.db")
    try:
        items = [OrderItem(1, "Хлеб", 100, 1)]
        await repo.create(1, "Аня", "", items, "old")
        repo._conn.execute("UPDATE order_rate_limits SET created_at='2000-01-01T00:00:00+00:00'")
        repo._conn.commit()
        await repo.create(2, "Аня", "", items, "current")
        await repo.forget(2)
        await repo.cleanup_rate_limits()
        assert repo._conn.execute("SELECT user_id FROM order_rate_limits").fetchall() == [(2,)]
    finally:
        await repo.close()


async def test_middleware_passes_original_update():
    from aiogram.types import Update

    update = Update(
        update_id=1,
        message={"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"}},
    )
    handler = AsyncMock()
    middleware = TrafficMiddleware()
    await middleware(handler, update, {})
    handler.assert_awaited_once_with(update, {})
