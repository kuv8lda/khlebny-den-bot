from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from khlebny_bot import main


@pytest.mark.parametrize("enabled", [False, True])
async def test_main_stops_worker_before_closing_resources(monkeypatch, enabled):
    closed = []
    settings = SimpleNamespace(
        fsm_db_path="unused", orders_path="unused", panel_enabled=enabled,
        panel_base_url="http://panel", panel_api_key=MagicMock(), panel_timeout_seconds=1,
        bot_token=MagicMock(), owner_ids=frozenset(), events_poll_interval=5,
    )
    monkeypatch.setattr(main, "Settings", lambda: settings)
    storage = AsyncMock()
    storage.close.side_effect = lambda: closed.append("storage")
    repo = AsyncMock()
    repo.close.side_effect = lambda: closed.append("orders")
    panel = AsyncMock()
    panel.aclose.side_effect = lambda: closed.append("panel")
    bot = AsyncMock()
    bot.session.close.side_effect = lambda: closed.append("bot")
    monkeypatch.setattr(main, "SqliteStorage", lambda _: storage)
    monkeypatch.setattr(main, "OrdersRepo", lambda _: repo)
    monkeypatch.setattr(main, "PanelClient", lambda *args: panel)
    monkeypatch.setattr(main, "Bot", lambda _: bot)
    dp = MagicMock()
    dp.start_polling = AsyncMock()
    monkeypatch.setattr(main, "Dispatcher", lambda **kwargs: dp)
    worker = MagicMock()

    async def run():
        closed.append("worker")

    worker.run = run
    factory = MagicMock(return_value=worker)
    monkeypatch.setattr(main, "EventsWorker", factory)
    await main.main()
    dp.start_polling.assert_awaited_once_with(bot, close_bot_session=False)
    if enabled:
        worker.stop.assert_called_once()
        assert closed == ["worker", "panel", "orders", "storage", "bot"]
    else:
        factory.assert_not_called()
        assert closed == ["orders", "storage", "bot"]
