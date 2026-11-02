"""Ранний ack и typing при повторной отправке заказа."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import make_callback

from khlebny_bot import checkout as co
from khlebny_bot import texts
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError, PanelOrderResult


@pytest.fixture
async def saved_order(tmp_path):
    orders = OrdersRepo(tmp_path / "orders.db")
    try:
        order, _ = await orders.create(
            1,
            "Аня",
            "",
            [OrderItem(1, "Медовик", 240000, 1)],
            "retry-token",
            pending_panel_sync=True,
        )
        yield orders, order
    finally:
        await orders.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_retry_ack_precedes_panel_and_typing_stops(saved_order, monkeypatch, failed):
    orders, order = saved_order
    cb = make_callback()
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
        assert kwargs["idempotency_key"] == "retry-token"
        if failed:
            raise PanelError("timeout")
        return PanelOrderResult(panel_order_id=99)

    panel.create_order.side_effect = send
    await co.on_order_retry(cb, co.RetryOrderCb(order_id=order.id), orders, panel)

    cb.answer.assert_awaited_once_with()
    typing.assert_called_once_with(
        bot=cb.bot,
        chat_id=cb.message.chat.id,
        message_thread_id=cb.message.message_thread_id,
    )
    sender.__aexit__.assert_awaited_once()
    panel.create_order.assert_awaited_once()
    saved = await orders.get(order.id, 1)
    assert saved.pending_panel_sync is failed
    assert len(await orders.list_for_user(1)) == 1
    if failed:
        cb.message.edit_text.assert_awaited_once_with(
            texts.PANEL_RETRY_UNAVAILABLE, reply_markup=co._retry_keyboard(order)
        )
        assert await orders.checkout_token(order.id, 1) == "retry-token"
    else:
        cb.message.edit_text.assert_awaited_once_with(
            texts.ORDER_CREATED.format(id=order.id), reply_markup=None
        )
        assert saved.panel_order_id == 99


async def test_retry_already_synced_acks_before_edit(saved_order):
    orders, order = saved_order
    await orders.mark_panel_synced(order.id)
    cb = make_callback()
    panel = AsyncMock(spec=PanelClient)

    async def edit(*args, **kwargs):
        cb.answer.assert_awaited_once_with()

    cb.message.edit_text.side_effect = edit
    await co.on_order_retry(cb, co.RetryOrderCb(order_id=order.id), orders, panel)
    cb.answer.assert_awaited_once_with()
    cb.message.edit_text.assert_awaited_once_with(
        texts.ORDER_ALREADY.format(id=order.id), reply_markup=None
    )
    panel.create_order.assert_not_awaited()


async def test_retry_missing_token_alerts_without_panel(saved_order, monkeypatch):
    orders, order = saved_order
    monkeypatch.setattr(orders, "checkout_token", AsyncMock(return_value=None))
    cb = make_callback()
    panel = AsyncMock(spec=PanelClient)
    await co.on_order_retry(cb, co.RetryOrderCb(order_id=order.id), orders, panel)
    cb.answer.assert_awaited_once_with(texts.PANEL_RETRY_UNAVAILABLE, show_alert=True)
    cb.message.edit_text.assert_not_awaited()
    panel.create_order.assert_not_awaited()


async def test_retry_without_message_still_syncs(saved_order, monkeypatch):
    orders, order = saved_order
    cb = make_callback()
    cb.message = None
    typing = MagicMock()
    monkeypatch.setattr(co.ChatActionSender, "typing", typing)
    panel = AsyncMock(spec=PanelClient)
    panel.create_order.return_value = PanelOrderResult(panel_order_id=99)
    await co.on_order_retry(cb, co.RetryOrderCb(order_id=order.id), orders, panel)
    cb.answer.assert_awaited_once_with()
    typing.assert_not_called()
    panel.create_order.assert_awaited_once()
    assert not (await orders.get(order.id, 1)).pending_panel_sync
