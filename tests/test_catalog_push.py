from aiohttp.test_utils import TestClient, TestServer

from khlebny_bot import catalog_push
from khlebny_bot.storage import SqliteStorage


async def test_catalog_push_auth_version_and_persistence(tmp_path, monkeypatch):
    token = "s" * 32
    old = [{"id": 1, "name": "Хлеб", "price_cents": 100}]
    new = [{"id": 1, "name": "Хлеб", "price_cents": 200}]
    monkeypatch.setattr(catalog_push, "CATALOG", list(old))
    path = tmp_path / "fsm.sqlite3"
    storage = SqliteStorage(path)
    await storage.sync_catalog(old)
    client = TestClient(TestServer(catalog_push.create_catalog_app(storage, token)))
    await client.start_server()
    try:
        payload = {"version": 2, "catalog": new}
        assert (await client.post("/catalog", json=payload)).status == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert (
            await client.post("/catalog", json={"version": 0, "catalog": new}, headers=headers)
        ).status == 400
        response = await client.post("/catalog", json=payload, headers=headers)
        assert response.status == 200
        assert (await response.json())["applied"] is True
        assert catalog_push.CATALOG == new
        response = await client.post(
            "/catalog", json={"version": 1, "catalog": old}, headers=headers
        )
        assert (await response.json())["applied"] is False
        assert catalog_push.CATALOG == new
    finally:
        await client.close()
        await storage.close()
    storage = SqliteStorage(path)
    try:
        assert await storage.load_catalog() == new
        assert not await storage.sync_catalog_version(old, 1)
        assert await storage.load_catalog() == new
    finally:
        await storage.close()
