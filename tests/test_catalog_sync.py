"""Catalogue changes invalidate carts, not completed order history."""
from helpers import make_state

from khlebny_bot.storage import SqliteStorage


async def test_saved_catalog_survives_restart(tmp_path):
    path = tmp_path / "fsm.sqlite3"
    storage = SqliteStorage(path)
    assert await storage.load_catalog() is None
    catalog = [{"id": 1, "name": "Хлеб", "price_cents": 12345}]
    await storage.sync_catalog(catalog)
    await storage.close()
    storage = SqliteStorage(path)
    try:
        assert await storage.load_catalog() == catalog
    finally:
        await storage.close()


async def test_catalog_sync_clears_legacy_cart_once(tmp_path):
    storage = SqliteStorage(tmp_path / "fsm.sqlite3")
    try:
        state = make_state(storage)
        await state.set_data({"cart": {"1": 2}, "checkout": {"name": "Анна"}, "other": 42})
        await state.set_state("Checkout:confirm")
        catalog = [{"id": 1, "name": "Медовик", "price_cents": 240000}]
        await storage.sync_catalog(catalog)
        assert await state.get_data() == {"other": 42}
        assert await state.get_state() is None
        await state.update_data(cart={"1": 1})
        await storage.sync_catalog(catalog)
        assert (await state.get_data())["cart"] == {"1": 1}
        await storage.sync_catalog([{**catalog[0], "price_cents": 250000}])
        assert "cart" not in await state.get_data()
    finally:
        await storage.close()
