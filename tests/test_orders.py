from datetime import UTC, datetime

import pytest

from khlebny_bot.config import Settings
from khlebny_bot.orders import OrderItem, OrdersRepo

ITEMS = [
    OrderItem(item_id=1, name="Ржаной хлеб на закваске", price_cents=14000, qty=2),
    OrderItem(item_id=3, name="Багет", price_cents=11000, qty=1),
]


@pytest.fixture
async def repo(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.sqlite3")
    yield repo
    await repo.close()


async def test_create_computes_total_in_cents_and_stores_items(repo):
    order, created = await repo.create(7, "Аня", "", ITEMS, "tok-1")
    assert created
    assert order.total_cents == 14000 * 2 + 11000
    assert order.status == "new"
    loaded = await repo.get(order.id, 7)
    assert loaded is not None
    assert loaded.items == tuple(ITEMS)
    assert loaded.total_cents == order.total_cents
    created_at = datetime.fromisoformat(loaded.created_at)
    assert created_at.tzinfo is not None and created_at.utcoffset() == UTC.utcoffset(None)


async def test_same_token_returns_same_order(repo):
    first, created1 = await repo.create(7, "Аня", "", ITEMS, "tok-1")
    second, created2 = await repo.create(7, "Аня", "", ITEMS, "tok-1")
    assert created1 and not created2
    assert second.id == first.id
    assert len(await repo.list_for_user(7)) == 1


async def test_empty_order_is_rejected(repo):
    with pytest.raises(ValueError):
        await repo.create(7, "Аня", "", [], "tok-1")


async def test_get_hides_foreign_order(repo):
    order, _ = await repo.create(7, "Аня", "", ITEMS, "tok-1")
    assert await repo.get(order.id, 8) is None
    assert await repo.get(999, 7) is None


async def test_list_is_newest_first_and_limited(repo):
    for n in range(7):
        await repo.create(7, "Аня", "", ITEMS, f"tok-{n}")
    ids = [o.id for o in await repo.list_for_user(7, limit=5)]
    assert len(ids) == 5
    assert ids == sorted(ids, reverse=True)


def test_orders_path_falls_back_to_fsm_path():
    s = Settings(_env_file=None, bot_token="t", fsm_db_path="data/fsm.sqlite3")
    assert s.orders_path == "data/fsm.sqlite3"
    s = Settings(_env_file=None, bot_token="t", orders_db_path="data/orders.sqlite3")
    assert s.orders_path == "data/orders.sqlite3"
