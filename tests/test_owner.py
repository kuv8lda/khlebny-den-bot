"""Этап D: сценарий владельца (уведомление, кнопки статусов, /owner, права)."""

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from helpers import make_callback, make_checkout_callback, make_message, make_state

from khlebny_bot import checkout as co
from khlebny_bot import owner, texts
from khlebny_bot.config import Settings, parse_owner_ids
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.owner import OwnerCb
from khlebny_bot.panel import PanelClient, PanelConflictError, PanelError, PanelOrderResult

BASE = "http://127.0.0.1:5000"
OWNER = 777
OWNERS = frozenset({OWNER})
ITEMS = [OrderItem(1, "Багет", 15000, 2), OrderItem(3, "Круассан", 9000, 1)]


@pytest.fixture
async def orders(tmp_path):
    repo = OrdersRepo(tmp_path / "o.sqlite3")
    yield repo
    await repo.close()


async def make_order(orders, *, status="new", panel_id=None, token="t1", comment="Без глютена"):
    order, _ = await orders.create(5, "Аня", comment, ITEMS, token)
    if panel_id is not None:
        await orders.set_panel_order_id(order.id, panel_id)
    if status != "new":
        await orders.set_status(order.id, status)
    got = await orders.get_by_id(order.id)
    assert got is not None
    return got


def press(order_id, status, user_id=OWNER):
    cb = make_callback(user_id=user_id)
    return cb, OwnerCb(order_id=order_id, status=status)


def panel_mock(*, returns=None, raises=None):
    client = AsyncMock(spec=PanelClient)
    if raises is not None:
        client.patch_status.side_effect = raises
    else:
        client.patch_status.return_value = returns
    return client


# --- PanelClient.patch_status ----------------------------------------------


@pytest.fixture
async def client():
    c = PanelClient(BASE, "secret-key", timeout=1)
    yield c
    await c.aclose()


@respx.mock
async def test_patch_status_sends_bearer_path_and_body(client):
    route = respx.patch(f"{BASE}/api/integrations/orders/12/status").mock(
        return_value=httpx.Response(200, json={"id": 12, "status": "paid"})
    )
    assert await client.patch_status(12, "paid") == "paid"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert request.read() == b'{"status":"paid"}'


@respx.mock
async def test_patch_status_200_without_body_returns_requested(client):
    respx.patch(f"{BASE}/api/integrations/orders/1/status").mock(return_value=httpx.Response(200))
    assert await client.patch_status(1, "shipped") == "shipped"


@respx.mock
async def test_patch_status_409_is_conflict_and_keeps_current_status(client):
    respx.patch(f"{BASE}/api/integrations/orders/1/status").mock(
        return_value=httpx.Response(409, json={"status": "cancelled"})
    )
    with pytest.raises(PanelConflictError) as info:
        await client.patch_status(1, "paid")
    assert info.value.current_status == "cancelled"


@respx.mock
async def test_patch_status_409_without_body(client):
    respx.patch(f"{BASE}/api/integrations/orders/1/status").mock(return_value=httpx.Response(409))
    with pytest.raises(PanelConflictError) as info:
        await client.patch_status(1, "paid")
    assert info.value.current_status is None


@respx.mock
@pytest.mark.parametrize("status", [401, 404, 422, 500])
async def test_patch_status_other_http_errors(client, status):
    respx.patch(f"{BASE}/api/integrations/orders/1/status").mock(
        return_value=httpx.Response(status)
    )
    with pytest.raises(PanelError) as info:
        await client.patch_status(1, "paid")
    assert not isinstance(info.value, PanelConflictError)


@respx.mock
async def test_patch_status_network_error(client):
    respx.patch(f"{BASE}/api/integrations/orders/1/status").mock(
        side_effect=httpx.ConnectError("down")
    )
    with pytest.raises(PanelError):
        await client.patch_status(1, "paid")


# --- Конфиг ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", set()), ("1", {1}), ("1, 2,3", {1, 2, 3}), ("1 2", {1, 2}), (" 5,5 ", {5})],
)
def test_parse_owner_ids(raw, expected):
    assert parse_owner_ids(raw) == expected


def test_owner_ids_from_settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "10, 20")
    assert Settings(_env_file=None).owner_ids == {10, 20}


def test_owner_ids_default_is_empty(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.delenv("OWNER_TELEGRAM_IDS", raising=False)
    assert Settings(_env_file=None).owner_ids == frozenset()


def test_owner_ids_garbage_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", "10, abc")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


# --- Репозиторий заказов ---------------------------------------------------


async def test_repo_migrates_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            name TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new', total_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL, checkout_token TEXT NOT NULL UNIQUE,
            pending_panel_sync INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE order_items (
            order_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
            name TEXT NOT NULL, price_cents INTEGER NOT NULL, qty INTEGER NOT NULL
        );
        INSERT INTO orders(user_id, name, total_cents, created_at, checkout_token)
        VALUES (5, 'Старый', 100, '2026-10-01T10:00:00+00:00', 'old');
        """
    )
    conn.commit()
    conn.close()

    repo = OrdersRepo(path)
    try:
        old = await repo.get_by_id(1)
        assert old is not None
        assert old.name == "Старый"
        assert old.panel_order_id is None
        await repo.set_panel_order_id(1, 42)
        assert (await repo.get_by_id(1)).panel_order_id == 42
    finally:
        await repo.close()


async def test_repo_status_counters_and_lookup(orders):
    await make_order(orders, token="a")
    await make_order(orders, token="b", status="paid", panel_id=2)
    assert await orders.get_by_id(999) is None
    assert await orders.count_by_status() == {"new": 1, "paid": 1}
    await orders.mark_panel_unsynced(1)
    assert await orders.count_unsynced() == 1


# --- Кнопки по FSM ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("new", ["paid", "cancelled"]),
        ("pending", ["paid", "cancelled"]),
        ("paid", ["shipped", "cancelled"]),
        ("shipped", ["completed"]),
        ("completed", []),
        ("cancelled", []),
    ],
)
async def test_buttons_follow_fsm(orders, status, expected):
    order = await make_order(orders, status=status, panel_id=9)
    markup = owner.owner_keyboard(order)
    got = (
        []
        if markup is None
        else [OwnerCb.unpack(row[0].callback_data).status for row in markup.inline_keyboard]
    )
    assert got == expected


async def test_button_callback_data_fits_telegram_limit(orders):
    order = await make_order(orders, status="shipped", panel_id=9)
    markup = owner.owner_keyboard(order)
    assert markup is not None
    for row in markup.inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64


async def test_no_buttons_without_panel_id(orders):
    order = await make_order(orders)
    assert owner.owner_keyboard(order) is None
    assert texts.OWNER_NOT_SYNCED in owner.render_owner_order(order)


# --- Уведомление владельцу -------------------------------------------------


async def test_card_has_all_fields(orders):
    order = await make_order(orders, panel_id=9)
    pages = (len(owner._owner_lines(order)) + 1) // 2
    text = "\n".join(owner.render_owner_order(order, page) for page in range(pages))
    assert f"№{order.id}" in text
    assert "ожидает оплаты" in text
    assert "Аня" in text
    assert "Без глютена" in text
    assert "Багет × 2" in text
    assert "Круассан × 1" in text
    assert "UTC" in text  # дата
    assert "390" in text  # 2*150 + 90 = 390 ₽
    assert texts.OWNER_NOT_SYNCED not in text


async def test_card_without_comment_shows_dash(orders):
    order = await make_order(orders, panel_id=9, comment="")
    assert texts.CHECKOUT_COMMENT_LINE.format(comment=texts.NO_COMMENT) in (
        owner.render_owner_order(order)
    )


async def test_notify_owners_sends_card_with_buttons_to_each_owner(orders):
    order = await make_order(orders, panel_id=9)
    bot = AsyncMock(spec=Bot)
    await owner.notify_owners(bot, {1, 2}, order)
    assert [c.args[0] for c in bot.send_message.await_args_list] == [1, 2]
    call = bot.send_message.await_args_list[0]
    assert call.args[1] == owner.render_owner_order(order)
    assert call.kwargs["reply_markup"] is not None


async def test_notify_owners_survives_blocked_owner(orders):
    order = await make_order(orders, panel_id=9)
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [TelegramForbiddenError(MagicMock(), "blocked"), None]
    await owner.notify_owners(bot, {1, 2}, order)
    assert bot.send_message.await_count == 2


async def _confirm(orders, notifier, panel_client, *, text_name="Аня"):
    state = make_state()
    await state.update_data(cart={"1": 2, "3": 1})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message(text_name), state)
    await co.on_skip(await make_checkout_callback(state, "skip"), state)
    saved = (await state.get_data(), await state.get_state())
    await co.on_confirm(
        await make_checkout_callback(state, "confirm"),
        state,
        orders,
        panel_client=panel_client,
        owner_notifier=notifier,
    )
    return state, saved


async def test_new_order_notifies_owner_with_panel_id(orders):
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.return_value = PanelOrderResult(panel_order_id=55, duplicate=False)
    notifier = AsyncMock()
    await _confirm(orders, notifier, panel)

    notifier.assert_awaited_once()
    notified = notifier.await_args.args[0]
    assert notified.panel_order_id == 55
    assert notified.name == "Аня"
    assert owner.owner_keyboard(notified) is not None


async def test_new_order_notifies_owner_even_if_panel_down(orders):
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.side_effect = PanelError("down")
    notifier = AsyncMock()
    await _confirm(orders, notifier, panel)

    notified = notifier.await_args.args[0]
    assert notified.panel_order_id is None
    assert owner.owner_keyboard(notified) is None


async def test_panel_id_given_as_digit_string_is_stored(orders):
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.return_value = PanelOrderResult(panel_order_id="55", duplicate=False)
    await _confirm(orders, None, panel)
    assert (await orders.get_by_id(1)).panel_order_id == 55


async def test_duplicate_confirm_does_not_notify_again(orders):
    notifier = AsyncMock()
    state, (data, fsm_state) = await _confirm(orders, notifier, None)
    await state.set_data(data)
    await state.set_state(fsm_state)
    await co.on_confirm(
        await make_checkout_callback(state, "confirm"), state, orders, owner_notifier=notifier
    )
    notifier.assert_awaited_once()


async def test_notifier_failure_does_not_break_checkout(orders):
    notifier = AsyncMock(side_effect=RuntimeError("boom"))
    await _confirm(orders, notifier, None)
    assert len(await orders.list_for_user(1, 10)) == 1


# --- Нажатие кнопки --------------------------------------------------------


async def test_press_patches_panel_and_updates_message(orders):
    order = await make_order(orders, panel_id=9)
    panel = panel_mock(returns="paid")
    cb, data = press(order.id, "paid")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    panel.patch_status.assert_awaited_once_with(9, "paid")
    assert (await orders.get_by_id(order.id)).status == "paid"
    cb.message.edit_text.assert_awaited_once()
    new_text = cb.message.edit_text.await_args.args[0]
    assert "оплачен" in new_text
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert [
        OwnerCb.unpack(r[0].callback_data).status
        for r in markup.inline_keyboard
        if r[0].callback_data.startswith("ow:")
    ] == [
        "shipped",
        "cancelled",
    ]
    cb.answer.assert_awaited_once_with()


async def test_final_status_removes_buttons(orders):
    order = await make_order(orders, status="shipped", panel_id=9)
    panel = panel_mock(returns="completed")
    cb, data = press(order.id, "completed")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert markup is not None
    assert all(b.callback_data.startswith("op:") for row in markup.inline_keyboard for b in row)
    assert "выполнен" in cb.message.edit_text.await_args.args[0]


async def test_conflict_409_shows_clear_text_and_keeps_status(orders):
    order = await make_order(orders, panel_id=9)
    panel = panel_mock(raises=PanelConflictError("409"))
    cb, data = press(order.id, "paid")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    cb.answer.assert_awaited_once_with()
    cb.message.answer.assert_awaited_once_with(texts.OWNER_CONFLICT)
    assert (await orders.get_by_id(order.id)).status == "new"
    cb.message.edit_text.assert_not_awaited()


async def test_conflict_with_known_current_status_refreshes_card(orders):
    order = await make_order(orders, panel_id=9)
    panel = panel_mock(raises=PanelConflictError("409", current_status="cancelled"))
    cb, data = press(order.id, "paid")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    cb.answer.assert_awaited_once_with()
    cb.message.answer.assert_awaited_once_with(texts.OWNER_CONFLICT)
    assert (await orders.get_by_id(order.id)).status == "cancelled"
    assert "отменён" in cb.message.edit_text.await_args.args[0]
    markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert markup is not None
    assert all(b.callback_data.startswith("op:") for row in markup.inline_keyboard for b in row)


async def test_panel_down_keeps_status(orders):
    order = await make_order(orders, panel_id=9)
    panel = panel_mock(raises=PanelError("down"))
    cb, data = press(order.id, "paid")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    cb.answer.assert_awaited_once_with()
    cb.message.answer.assert_awaited_once_with(texts.OWNER_PANEL_DOWN)
    assert (await orders.get_by_id(order.id)).status == "new"


async def test_stale_button_does_not_call_panel(orders):
    order = await make_order(orders, status="shipped", panel_id=9)
    panel = panel_mock(returns="paid")
    cb, data = press(order.id, "paid")  # из shipped в paid нельзя

    await owner.on_status(cb, data, orders, panel, OWNERS)

    panel.patch_status.assert_not_awaited()
    cb.answer.assert_awaited_once_with(texts.OWNER_STALE, show_alert=True)
    assert "передан в доставку" in cb.message.edit_text.await_args.args[0]


async def test_order_without_panel_id_cannot_change_status(orders):
    order = await make_order(orders)
    panel = panel_mock(returns="paid")
    cb, data = press(order.id, "paid")

    await owner.on_status(cb, data, orders, panel, OWNERS)

    panel.patch_status.assert_not_awaited()
    cb.answer.assert_awaited_once_with(texts.OWNER_NO_PANEL, show_alert=True)


async def test_unknown_order(orders):
    cb, data = press(999, "paid")
    await owner.on_status(cb, data, orders, panel_mock(returns="paid"), OWNERS)
    cb.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)


# --- Права -----------------------------------------------------------------


async def test_non_owner_press_is_denied_and_changes_nothing(orders):
    order = await make_order(orders, panel_id=9)
    panel = panel_mock(returns="paid")
    cb, data = press(order.id, "paid", user_id=123)

    await owner.on_status(cb, data, orders, panel, OWNERS)

    cb.answer.assert_awaited_once_with(texts.NO_ACCESS, show_alert=True)
    panel.patch_status.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()
    assert (await orders.get_by_id(order.id)).status == "new"


async def test_press_with_no_owners_configured_is_denied(orders):
    order = await make_order(orders, panel_id=9)
    cb, data = press(order.id, "paid")
    await owner.on_status(cb, data, orders, panel_mock(returns="paid"))
    cb.answer.assert_awaited_once_with(texts.NO_ACCESS, show_alert=True)


async def test_owner_command_denied_for_non_owner(orders):
    msg = make_message("/owner", user_id=123)
    await owner.cmd_owner(msg, orders, OWNERS)
    msg.answer.assert_awaited_once_with(texts.NO_ACCESS)


# --- /owner ----------------------------------------------------------------


async def test_owner_summary_counts(orders):
    await make_order(orders, token="a", panel_id=1)  # new -> считается как ожидает оплаты
    await make_order(orders, token="b", status="pending", panel_id=2)
    await make_order(orders, token="c", status="paid", panel_id=3)
    await make_order(orders, token="d", status="shipped", panel_id=4)
    await make_order(orders, token="e", status="shipped", panel_id=5)
    await make_order(orders, token="f", status="completed", panel_id=6)
    await make_order(orders, token="g", status="cancelled", panel_id=7)

    msg = make_message("/owner", user_id=OWNER)
    await owner.cmd_owner(msg, orders, OWNERS)

    text = msg.answer.await_args.args[0]
    assert "Ожидают оплаты: 2" in text
    assert "В работе (оплачены или в доставке): 3" in text
    assert "Не отправлены" not in text


async def test_owner_summary_mentions_unsynced(orders):
    order = await make_order(orders)
    await orders.mark_panel_unsynced(order.id)
    msg = make_message("/owner", user_id=OWNER)
    await owner.cmd_owner(msg, orders, OWNERS)
    assert "Не отправлены в панель: 1" in msg.answer.await_args.args[0]


async def test_owner_summary_empty(orders):
    msg = make_message("/owner", user_id=OWNER)
    await owner.cmd_owner(msg, orders, OWNERS)
    text = msg.answer.await_args.args[0]
    assert "Ожидают оплаты: 0" in text
    assert "В работе (оплачены или в доставке): 0" in text
