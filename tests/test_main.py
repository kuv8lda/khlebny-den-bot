from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from khlebny_bot import main
from khlebny_bot.panel import PanelError


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("with_owner", [False, True])
@pytest.mark.parametrize("panel_down", [False, True])
@pytest.mark.parametrize("saved", [False, True])
async def test_main_stops_worker_before_closing_resources(
    monkeypatch, caplog, enabled, with_owner, panel_down, saved
):
    closed = []
    fallback = [{"id": 1, "name": "Хлеб", "price_cents": 100}]
    monkeypatch.setattr(main, "CATALOG", list(fallback))
    settings = SimpleNamespace(
        fsm_db_path="unused",
        orders_path="unused",
        panel_enabled=enabled,
        panel_base_url="http://panel",
        panel_api_key=MagicMock(),
        panel_timeout_seconds=1,
        catalog_ttl_seconds=60,
        catalog_push_token=MagicMock(get_secret_value=lambda: ""),
        bot_token=MagicMock(),
        owner_ids=frozenset({777}) if with_owner else frozenset(),
        events_poll_interval=5,
    )
    monkeypatch.setattr(main, "Settings", lambda: settings)
    storage = AsyncMock()
    saved_catalog = [{"id": 1, "name": "Хлеб", "price_cents": 250}]
    storage.load_catalog.return_value = saved_catalog if saved else None
    storage.close.side_effect = lambda: closed.append("storage")
    repo = AsyncMock()
    repo.close.side_effect = lambda: closed.append("orders")
    panel = AsyncMock()
    panel.fetch_catalog.return_value = [{"id": 42, "name": "Багет", "price_cents": 11000}]
    if panel_down:
        panel.fetch_catalog.side_effect = PanelError("offline")
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
    competitor_repo = AsyncMock()
    competitor_repo.close.side_effect = lambda: closed.append("competitor_repo")
    monkeypatch.setattr(main, "CompetitorRepo", lambda _: competitor_repo)
    competitor_worker = MagicMock()

    async def competitor_run():
        closed.append("competitor_worker")

    competitor_worker.run = competitor_run
    competitor_factory = MagicMock(return_value=competitor_worker)
    monkeypatch.setattr(main, "CompetitorWorker", competitor_factory)
    await main.main()
    bot.set_my_commands.assert_awaited_once()
    commands = bot.set_my_commands.call_args.args[0]
    assert "orders" in [command.command for command in commands]
    if enabled and with_owner:
        competitor_worker.stop.assert_called_once()
        assert closed.index("competitor_worker") < closed.index("competitor_repo")
        assert closed.index("competitor_repo") < closed.index("panel")
        closed = [item for item in closed if not item.startswith("competitor_")]
    else:
        competitor_factory.assert_not_called()
    dp.start_polling.assert_awaited_once_with(bot, close_bot_session=False)
    if enabled:
        panel.fetch_catalog.assert_awaited_once()
        if panel_down:
            expected = saved_catalog if saved else fallback
            assert main.CATALOG == expected
            storage.sync_catalog.assert_awaited_once_with(expected)
            message = "using last saved catalog" if saved else "using fallback catalog"
            assert message in caplog.text
        else:
            assert main.CATALOG == panel.fetch_catalog.return_value
        worker.stop.assert_called_once()
        assert closed == ["worker", "panel", "orders", "storage", "bot"]
    else:
        factory.assert_not_called()
        assert closed == ["orders", "storage", "bot"]
