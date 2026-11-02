import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from khlebny_bot import texts
from khlebny_bot.config import Settings
from khlebny_bot.events import EventsWorker
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError, PanelEvent

URL = "http://panel/api/integrations/events"


def event(id=1, status="paid", order_id=90):
    return PanelEvent(id=id, order_id=order_id, status=status, created_at="2026-10-22T12:00:00Z")


@pytest.fixture
async def setup(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.sqlite3")
    order, _ = await repo.create(42, "Имя", "", [OrderItem(1, "Хлеб", 100, 1)], "token")
    await repo.set_panel_order_id(order.id, 90)
    bot = AsyncMock()
    panel = AsyncMock(spec=PanelClient)
    worker = EventsWorker(bot, panel, repo, interval=0.001)
    yield worker, repo, bot, panel, order
    await repo.close()


@respx.mock
async def test_fetch_events_cursor_and_auth():
    route = respx.get(URL, params={"since_id": 7, "limit": 12}).respond(
        200, json=[event(8).model_dump()]
    )
    client = PanelClient("http://panel", "secret")
    try:
        assert await client.fetch_events(7, 12) == [event(8)]
        assert route.calls.last.request.headers["Authorization"] == "Bearer secret"
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.parametrize(
    "payload", [{}, [dict(id="1")], [event(2).model_dump(), event(1).model_dump()]]
)
async def test_bad_events_response(payload):
    respx.get(URL).respond(200, json=payload)
    client = PanelClient("http://panel", "secret")
    try:
        with pytest.raises(PanelError):
            await client.fetch_events(0)
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.parametrize("failure", [401, 500, "json", "network"])
async def test_fetch_failures(failure):
    route = respx.get(URL)
    if failure == "network":
        route.mock(side_effect=httpx.ConnectError("offline"))
    elif failure == "json":
        route.respond(200, text="not json")
    else:
        route.respond(failure)
    client = PanelClient("http://panel", "secret")
    try:
        with pytest.raises(PanelError):
            await client.fetch_events(0)
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", list(texts.EVENT_STATUS_NAMES))
async def test_notification_and_status(setup, status):
    worker, repo, bot, panel, order = setup
    panel.fetch_events.return_value = [event(status=status)]
    await worker.poll_once()
    panel.fetch_events.assert_awaited_once_with(0, limit=50)
    bot.send_message.assert_awaited_once_with(
        chat_id=42,
        text=texts.ORDER_STATUS_NOTIFICATION.format(
            id=order.id, status=texts.EVENT_STATUS_NAMES[status]
        ),
    )
    assert (await repo.get(order.id, 42)).status == status
    assert await repo.event_cursor() == 1


async def test_repeated_events_and_restart(setup, tmp_path):
    worker, repo, bot, panel, _ = setup
    panel.fetch_events.return_value = [event(1), event(2, "shipped")]
    await worker.poll_once()
    await worker.poll_once()
    assert bot.send_message.await_count == 2
    panel.fetch_events.assert_awaited_with(2, limit=50)
    reopened = OrdersRepo(tmp_path / "orders.sqlite3")
    try:
        await EventsWorker(bot, panel, reopened).poll_once()
        assert await reopened.event_cursor() == 2
        assert bot.send_message.await_count == 2
    finally:
        await reopened.close()


async def test_unknown_order_and_status_are_skipped(setup):
    worker, repo, bot, panel, _ = setup
    panel.fetch_events.return_value = [event(1, order_id=999), event(2, "future"), event(3)]
    await worker.poll_once()
    assert bot.send_message.await_count == 1
    assert await repo.event_cursor() == 3


async def test_transient_telegram_failure_keeps_cursor(setup):
    worker, repo, bot, panel, _ = setup
    panel.fetch_events.return_value = [event(1), event(2, "shipped")]
    bot.send_message.side_effect = [
        None,
        TelegramNetworkError(method=SendMessage(chat_id=42, text="test"), message="offline"),
        None,
    ]
    with pytest.raises(TelegramNetworkError):
        await worker.poll_once()
    assert await repo.event_cursor() == 1
    await worker.poll_once()
    assert await repo.event_cursor() == 2
    assert bot.send_message.await_count == 3


async def test_blocked_bot_does_not_block_stream(setup):
    worker, repo, bot, panel, _ = setup
    panel.fetch_events.return_value = [event(1), event(2)]
    bot.send_message.side_effect = [
        TelegramForbiddenError(method=SendMessage(chat_id=42, text="test"), message="blocked"),
        None,
    ]
    await worker.poll_once()
    assert await repo.event_cursor() == 2


async def test_worker_survives_panel_failure_and_stops(setup):
    worker, repo, bot, panel, _ = setup

    async def fetch(*args, **kwargs):
        if panel.fetch_events.await_count == 1:
            raise PanelError("offline")
        worker.stop()
        return []

    panel.fetch_events.side_effect = fetch
    await asyncio.wait_for(worker.run(), timeout=1)
    assert panel.fetch_events.await_count == 2
    assert await repo.event_cursor() == 0
    bot.send_message.assert_not_awaited()


async def test_stop_interrupts_sleep(setup):
    worker, _, _, panel, _ = setup
    panel.fetch_events.return_value = []
    worker.interval = 3600
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.01)
    worker.stop()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.parametrize("interval", [0, -1, "nan", "inf"])
def test_invalid_poll_interval(interval):
    with pytest.raises(ValueError):
        Settings(_env_file=None, bot_token="test", events_poll_interval=interval)


def test_default_poll_interval():
    assert Settings(_env_file=None, bot_token="test").events_poll_interval == 5
