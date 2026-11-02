"""Старые кнопки оформления и повторная отправка сохранённых заказов."""

from unittest.mock import AsyncMock

import pytest
from helpers import make_callback, make_checkout_callback, make_message, make_state

from khlebny_bot import checkout as co
from khlebny_bot import handlers as h
from khlebny_bot import texts
from khlebny_bot.orders import OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError, PanelOrderResult


async def prepare(state, step="confirm"):
    await state.update_data(cart={"1": 2})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message("Аня"), state)
    if step == "confirm":
        await co.on_comment(make_message("Комментарий"), state)


@pytest.fixture
async def orders(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.db")
    yield repo
    await repo.close()


@pytest.mark.parametrize("action", ["skip", "confirm", "cancel"])
async def test_old_button_cannot_change_new_checkout(action, orders):
    state = make_state()
    step = "comment" if action == "skip" else "confirm"
    await prepare(state, step)
    old = await make_checkout_callback(state, action)
    await h.cmd_cancel(make_message("/cancel"), state)
    await prepare(state, step)
    before = await state.get_data(), await state.get_state()
    if action == "confirm":
        await co.on_confirm(old, state, orders)
    elif action == "cancel":
        await co.on_cancel_button(old, state)
    else:
        await co.on_skip(old, state)
    assert (await state.get_data(), await state.get_state()) == before
    assert await orders.list_for_user(1) == []
    old.answer.assert_awaited_once_with(texts.CHECKOUT_STALE, show_alert=True)
    old.message.edit_text.assert_not_awaited()
    old.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.parametrize("data", ["o2:confirm", "o2:confirm:", None])
async def test_tokenless_confirm_is_stale(data, orders):
    state = make_state()
    await prepare(state)
    cb = make_callback()
    cb.data = data
    before = await state.get_data()
    await co.on_confirm(cb, state, orders)
    assert await state.get_data() == before
    assert await orders.list_for_user(1) == []
    cb.answer.assert_awaited_once_with(texts.CHECKOUT_STALE, show_alert=True)


async def test_buttons_include_checkout_token_and_fit_telegram_limit():
    state = make_state()
    await prepare(state, "comment")
    token = (await state.get_data())["checkout"]["token"]
    keyboards = [
        co._skip_keyboard(token),
        co.render_confirm(co.snapshot_cart({"1": 2}), "Аня", "", token)[1],
    ]
    for markup in keyboards:
        for row in markup.inline_keyboard:
            for button in row:
                cb_type = (
                    co.CheckoutViewCb if button.callback_data.startswith("cv:") else h.CheckoutCb
                )
                assert cb_type.unpack(button.callback_data).token == token
                assert len(button.callback_data.encode()) <= 64


async def failed_order(state, orders):
    await prepare(state)
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.side_effect = PanelError("timeout")
    cb = await make_checkout_callback(state, "confirm")
    await co.on_confirm(cb, state, orders, panel)
    order = (await orders.list_for_user(1))[0]
    return cb, panel, order


async def test_failure_replaces_cancel_with_order_retry(orders):
    state = make_state()
    cb, _, order = await failed_order(state, orders)
    assert order.pending_panel_sync
    assert await state.get_state() is None
    assert (await state.get_data())["cart"] == {}
    assert "checkout" not in await state.get_data()
    call = cb.message.edit_text.call_args
    assert call.args[0] == texts.PANEL_ORDER_UNAVAILABLE.format(id=order.id)
    buttons = call.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == len(buttons[0]) == 1
    assert buttons[0][0].text == texts.BTN_RETRY_ORDER
    assert co.RetryOrderCb.unpack(buttons[0][0].callback_data).order_id == order.id
    msg = make_message()
    await co.on_order_open(make_callback_for_message(msg), co.OrderCb(order_id=order.id), orders)
    order_buttons = msg.answer.call_args.kwargs["reply_markup"].inline_keyboard
    assert order_buttons[-1] == call.kwargs["reply_markup"].inline_keyboard[0]


def make_callback_for_message(message):
    cb = make_callback()
    cb.message = message
    return cb


async def test_retry_survives_restart_and_does_not_touch_new_checkout(tmp_path):
    path = tmp_path / "orders.db"
    orders = OrdersRepo(path)
    state = make_state()
    try:
        _, panel, order = await failed_order(state, orders)
        first_call = panel.create_order.call_args
    finally:
        await orders.close()
    orders = OrdersRepo(path)
    try:
        await prepare(state, "comment")
        before = await state.get_data(), await state.get_state()
        panel.create_order.side_effect = None
        panel.create_order.return_value = PanelOrderResult(panel_order_id=99)
        button = co.RetryOrderCb(order_id=order.id)
        await co.on_order_retry(make_callback(), button, orders, panel)
        assert panel.create_order.call_args == first_call
        assert (await state.get_data(), await state.get_state()) == before
        saved = await orders.get(order.id, 1)
        assert saved.panel_order_id == 99 and not saved.pending_panel_sync
        await co.on_order_retry(make_callback(), button, orders, panel)
        assert panel.create_order.await_count == 2
        assert len(await orders.list_for_user(1)) == 1
    finally:
        await orders.close()


@pytest.mark.parametrize("user_id", [2, 1])
async def test_retry_rejects_foreign_or_deleted_order(user_id, orders):
    _, _, order = await failed_order(make_state(), orders)
    if user_id == 1:
        await orders.forget(1)
    panel = AsyncMock(spec=PanelClient)
    cb = make_callback(user_id=user_id)
    await co.on_order_retry(cb, co.RetryOrderCb(order_id=order.id), orders, panel)
    panel.create_order.assert_not_awaited()
    cb.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)


@pytest.mark.parametrize("enabled", [True, False])
async def test_retry_failure_keeps_saved_order_and_key(enabled, orders):
    _, panel, order = await failed_order(make_state(), orders)
    token = await orders.checkout_token(order.id, 1)
    cb = make_callback()
    await co.on_order_retry(
        cb, co.RetryOrderCb(order_id=order.id), orders, panel if enabled else None
    )
    if enabled:
        cb.answer.assert_awaited_once_with()
        cb.message.edit_text.assert_awaited_once_with(
            texts.PANEL_RETRY_UNAVAILABLE, reply_markup=co._retry_keyboard(order)
        )
    else:
        cb.answer.assert_awaited_once_with(texts.PANEL_RETRY_UNAVAILABLE, show_alert=True)
        cb.message.edit_text.assert_not_awaited()
    assert (await orders.get(order.id, 1)).pending_panel_sync
    assert await orders.checkout_token(order.id, 1) == token
    assert len(await orders.list_for_user(1)) == 1
