from unittest.mock import AsyncMock

import pytest
import respx
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from helpers import make_message

from khlebny_bot.competitor import (
    CompetitorRepo,
    CompetitorWorker,
    cmd_competitor,
    significant,
)
from khlebny_bot.panel import CompetitorPrice, PanelClient, PanelError


def price(cents=240000, **kwargs):
    return CompetitorPrice(
        competitor_id=1, competitor_name="X", product_id=2,
        product_name="Медовик", price_cents=cents, own_price_cents=240000, **kwargs,
    )


@pytest.mark.parametrize("old,new,expected", [
    (240000, 216000, True), (240000, 264000, True),
    (240000, 230400, False), (240000, 228000, False),
    (240000, 252000, False), (240000, 240000, False),
    (240000, 227999, True), (0, 100, False), (100, 0, True),
])
def test_threshold(old, new, expected):
    assert significant(old, new) is expected


@pytest.fixture
async def setup(tmp_path):
    repo = CompetitorRepo(tmp_path / "prices.sqlite3")
    bot = AsyncMock()
    panel = AsyncMock(spec=PanelClient)
    worker = CompetitorWorker(bot, panel, repo, frozenset({777, 888}), interval=0.001)
    yield worker, repo, bot, panel
    await repo.close()


@pytest.mark.parametrize("new,count", [(216000, 2), (264000, 2), (230400, 0), (228000, 0)])
async def test_notifications(setup, new, count):
    worker, _, bot, panel = setup
    panel.fetch_competitor_prices.return_value = [price()]
    await worker.poll_once()
    bot.send_message.assert_not_awaited()
    panel.fetch_competitor_prices.return_value = [price(new)]
    await worker.poll_once()
    assert bot.send_message.await_count == count
    if count:
        text = bot.send_message.call_args.kwargs["text"]
        assert "Конкурент X: Медовик" in text
        assert "10.00%" in text
        assert "Ваша цена: 2 400 ₽" in text
        assert {c.kwargs["chat_id"] for c in bot.send_message.call_args_list} == {777, 888}
    await worker.poll_once()
    assert bot.send_message.await_count == count


async def test_baseline_survives_restart(setup, tmp_path):
    worker, _, bot, panel = setup
    panel.fetch_competitor_prices.return_value = [price()]
    await worker.poll_once()
    reopened = CompetitorRepo(tmp_path / "prices.sqlite3")
    try:
        panel.fetch_competitor_prices.return_value = [price(216000)]
        await CompetitorWorker(bot, panel, reopened, frozenset({777})).poll_once()
        bot.send_message.assert_awaited_once()
    finally:
        await reopened.close()


async def test_retry_only_failed_owner(setup):
    worker, repo, bot, panel = setup
    panel.fetch_competitor_prices.return_value = [price()]
    await worker.poll_once()
    panel.fetch_competitor_prices.return_value = [price(216000)]
    bot.send_message.side_effect = [TelegramNetworkError(
        method=SendMessage(chat_id=777, text="test"), message="offline",
    ), None, None]
    await worker.poll_once()
    assert await repo.previous(777, price()) == 240000
    assert await repo.previous(888, price()) == 216000
    await worker.poll_once()
    assert [c.kwargs["chat_id"] for c in bot.send_message.call_args_list] == [777, 888, 777]


async def test_worker_recovers_from_panel_failure(setup):
    worker, _, bot, panel = setup

    async def fetch():
        if panel.fetch_competitor_prices.await_count == 1:
            raise PanelError("offline")
        worker.stop()
        return []

    panel.fetch_competitor_prices.side_effect = fetch
    import asyncio
    await asyncio.wait_for(worker.run(), timeout=1)
    assert panel.fetch_competitor_prices.await_count == 2
    bot.send_message.assert_not_awaited()


async def test_command_returns_prices():
    panel = AsyncMock(spec=PanelClient)
    panel.fetch_competitor_prices.return_value = [price(216000)]
    message = make_message("/competitor", user_id=777)
    await cmd_competitor(message, panel, frozenset({777}))
    text = message.answer.call_args.args[0]
    assert "Медовик — 2 160 ₽" in text
    assert "Конкурент X" in text
    assert "Ваша цена: 2 400 ₽" in text


async def test_command_denies_non_owner():
    panel = AsyncMock(spec=PanelClient)
    message = make_message("/competitor", user_id=42)
    await cmd_competitor(message, panel, frozenset({777}))
    panel.fetch_competitor_prices.assert_not_awaited()
    assert "доступ" in message.answer.call_args.args[0].lower()


@pytest.mark.parametrize("state", ["disabled", "empty", "offline"])
async def test_command_unavailable(state):
    panel = AsyncMock(spec=PanelClient)
    panel.fetch_competitor_prices.return_value = []
    if state == "offline":
        panel.fetch_competitor_prices.side_effect = PanelError("offline")
    message = make_message("/competitor", user_id=777)
    await cmd_competitor(message, None if state == "disabled" else panel, frozenset({777}))
    message.answer.assert_awaited_once()


@respx.mock
async def test_api_contract():
    route = respx.get("http://panel/api/integrations/competitor-prices").respond(
        200, json=[price().model_dump()],
    )
    client = PanelClient("http://panel", "secret")
    try:
        assert await client.fetch_competitor_prices() == [price()]
        assert route.calls.last.request.headers["Authorization"] == "Bearer secret"
    finally:
        await client.aclose()


@respx.mock
@pytest.mark.parametrize("payload", [
    {}, [{"price_cents": -1}], [price().model_dump(), price().model_dump()],
    [{**price().model_dump(), "price_cents": "240000"}],
])
async def test_api_rejects_invalid_prices(payload):
    respx.get("http://panel/api/integrations/competitor-prices").respond(200, json=payload)
    client = PanelClient("http://panel", "secret")
    try:
        with pytest.raises(PanelError):
            await client.fetch_competitor_prices()
    finally:
        await client.aclose()
