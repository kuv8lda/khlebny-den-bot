"""Ранний ack подтверждения и typing на время отправки в панель."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import make_checkout_callback, make_state

from khlebny_bot import checkout as co
from khlebny_bot import texts
from khlebny_bot.orders import OrderItem, OrderRateLimitError, OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError, PanelOrderResult


@pytest.mark.parametrize("failed", [False, True])
async def test_confirm_ack_precedes_storage_and_panel(tmp_path, monkeypatch, failed):
    state = make_state()
    await state.update_data(
        cart={"1": 1},
        checkout={
            "token": "test-token",
            "name": "Аня",
            "comment": "",
            "items": [vars(OrderItem(1, "Медовик", 240000, 1))],
        },
    )
    await state.set_state(co.Checkout.confirm)
    cb = await make_checkout_callback(state, "confirm")
    sender = MagicMock()
    sender.__aenter__ = AsyncMock()
    sender.__aexit__ = AsyncMock(return_value=False)
    typing = MagicMock(return_value=sender)
    monkeypatch.setattr(co.ChatActionSender, "typing", typing)
    panel = AsyncMock(spec=PanelClient)

    async def send(*args, **kwargs):
        cb.answer.assert_awaited_once_with()
        sender.__aenter__.assert_awaited_once()
        sender.__aexit__.assert_not_awaited()
        if failed:
            raise PanelError("timeout")
        return PanelOrderResult(panel_order_id=99)

    panel.create_order.side_effect = send
    orders = OrdersRepo(tmp_path / "orders.db")
    original_create = orders.create

    async def create(*args, **kwargs):
        cb.answer.assert_awaited_once_with()
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(orders, "create", create)
    try:
        await co.on_confirm(cb, state, orders, panel)
        cb.answer.assert_awaited_once_with()
        typing.assert_called_once_with(
            bot=cb.bot,
            chat_id=cb.message.chat.id,
            message_thread_id=cb.message.message_thread_id,
        )
        sender.__aexit__.assert_awaited_once()
        (order,) = await orders.list_for_user(1)
        assert order.pending_panel_sync is failed
        result = cb.message.edit_text.call_args
        if failed:
            assert result.args[0] == texts.PANEL_ORDER_UNAVAILABLE.format(id=order.id)
            button = result.kwargs["reply_markup"].inline_keyboard[0][0]
            assert co.RetryOrderCb.unpack(button.callback_data).order_id == order.id
        else:
            assert result.args[0] == texts.ORDER_CREATED.format(id=order.id)
    finally:
        await orders.close()


@pytest.mark.parametrize("is_owner", [False, True])
async def test_confirm_order_limit_owner_exemption(tmp_path, is_owner):
    orders = OrdersRepo(tmp_path / "orders.db")
    try:
        for number in range(6):
            state = make_state()
            await state.update_data(
                cart={"1": 1},
                checkout={
                    "token": f"token-{number}",
                    "name": "Аня",
                    "items": [vars(OrderItem(1, "Медовик", 240000, 1))],
                },
            )
            await state.set_state(co.Checkout.confirm)
            cb = await make_checkout_callback(state, "confirm")
            await co.on_confirm(
                cb, state, orders, owner_ids=frozenset({1}) if is_owner else frozenset()
            )
            if number == 5 and not is_owner:
                cb.message.answer.assert_awaited_once_with(texts.ORDER_RATE_LIMIT)
                assert await state.get_state() == co.Checkout.confirm.state
            else:
                cb.message.answer.assert_not_awaited()
        assert len(await orders.list_for_user(1, limit=10)) == (6 if is_owner else 5)
    finally:
        await orders.close()


async def test_confirm_rate_limit_uses_message_after_ack():
    state = make_state()
    checkout = {
        "token": "test-token",
        "name": "Аня",
        "items": [vars(OrderItem(1, "Медовик", 240000, 1))],
    }
    await state.update_data(cart={"1": 1}, checkout=checkout)
    await state.set_state(co.Checkout.confirm)
    cb = await make_checkout_callback(state, "confirm")
    orders = AsyncMock(spec=OrdersRepo)

    async def limited(*args, **kwargs):
        cb.answer.assert_awaited_once_with()
        raise OrderRateLimitError()

    orders.create.side_effect = limited
    await co.on_confirm(cb, state, orders)
    cb.answer.assert_awaited_once_with()
    cb.message.answer.assert_awaited_once_with(texts.ORDER_RATE_LIMIT)
    cb.message.edit_text.assert_not_awaited()
    assert await state.get_state() == co.Checkout.confirm.state
    assert (await state.get_data())["checkout"] == checkout
