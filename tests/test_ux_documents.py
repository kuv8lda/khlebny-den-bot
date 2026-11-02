"""Пагинация приватности и служебных карточек без потери данных."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import make_callback, make_message, make_state

from khlebny_bot import owner, privacy, texts
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.panel import PanelClient
from khlebny_bot.ui import one_line


@pytest.mark.parametrize("document", ["privacy", "local", "panel", "failed"])
async def test_privacy_pages_preserve_entire_text_and_ack_before_edit(document):
    original = privacy._documents()[document][1]
    collected = []
    page = 0
    while True:
        cb = make_callback()

        async def edit(*args, _cb=cb, **kwargs):
            _cb.answer.assert_awaited_once_with()

        cb.message.edit_text.side_effect = edit
        await privacy.on_privacy_page(cb, privacy.PrivacyPageCb(document=document, page=page))
        text = cb.message.edit_text.call_args.args[0]
        assert len(text.splitlines()) <= 4
        collected.extend(text.splitlines()[1:])
        markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
        buttons = [] if markup is None else [b for row in markup.inline_keyboard for b in row]
        for button in buttons:
            assert len(button.callback_data.encode()) <= 64
        next_buttons = [b for b in buttons if b.text == texts.BTN_NEXT]
        cb.answer.assert_awaited_once_with()
        if not next_buttons:
            break
        page = privacy.PrivacyPageCb.unpack(next_buttons[0].callback_data).page
        assert page < 20
    assert one_line(" ".join(collected)) == one_line(original)


async def test_privacy_invalid_document_does_not_edit():
    cb = make_callback()
    await privacy.on_privacy_page(cb, privacy.PrivacyPageCb(document="unknown"))
    cb.answer.assert_awaited_once()
    assert cb.answer.call_args.kwargs["show_alert"]
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.parametrize("page", [-100, 999])
def test_privacy_page_bounds(page):
    text, _ = privacy.render_privacy("privacy", page)
    assert len(text.splitlines()) <= 4
    assert "1/4" in text if page < 0 else "4/4" in text


async def test_forget_typing_surrounds_panel_request(monkeypatch, tmp_path):
    orders = OrdersRepo(tmp_path / "orders.db")
    state = make_state()
    message = make_message()
    sender = MagicMock()
    sender.__aenter__ = AsyncMock()
    sender.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(privacy.ChatActionSender, "typing", MagicMock(return_value=sender))
    panel = AsyncMock(spec=PanelClient)

    async def forget(user_id):
        assert user_id == 1
        sender.__aenter__.assert_awaited_once()
        sender.__aexit__.assert_not_awaited()

    panel.forget.side_effect = forget
    try:
        await privacy.cmd_forget(message, state, orders, panel)
        sender.__aexit__.assert_awaited_once()
        message.answer.assert_awaited_once()
    finally:
        await orders.close()


@pytest.fixture
async def card(tmp_path):
    orders = OrdersRepo(tmp_path / "orders.db")
    try:
        order, _ = await orders.create(
            1,
            "Аня",
            "Без\nсахара",
            [OrderItem(i, f"Товар {i}", i * 100, 1) for i in range(1, 9)],
            "token",
        )
        yield orders, order
    finally:
        await orders.close()


async def test_owner_pages_preserve_all_fields_with_early_ack(card):
    orders, order = card
    pages = (len(owner._owner_lines(order)) + 1) // 2
    collected = []
    for page in range(pages):
        cb = make_callback()

        async def edit(*args, _cb=cb, **kwargs):
            _cb.answer.assert_awaited_once_with()

        cb.message.edit_text.side_effect = edit
        await owner.on_owner_page(
            cb, owner.OwnerPageCb(order_id=order.id, page=page), orders, frozenset({1})
        )
        text = cb.message.edit_text.call_args.args[0]
        assert len(text.splitlines()) <= 4
        collected.extend(text.splitlines()[1:-1])
        markup = cb.message.edit_text.call_args.kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
    assert collected == owner._owner_lines(order)


async def test_owner_page_denies_non_owner_before_loading_card(card, monkeypatch):
    orders, order = card
    lookup = AsyncMock(wraps=orders.get_by_id)
    monkeypatch.setattr(orders, "get_by_id", lookup)
    cb = make_callback(user_id=2)
    await owner.on_owner_page(cb, owner.OwnerPageCb(order_id=order.id), orders, frozenset({1}))
    cb.answer.assert_awaited_once_with(texts.NO_ACCESS, show_alert=True)
    lookup.assert_not_awaited()
    cb.message.edit_text.assert_not_awaited()


async def test_owner_missing_order_answers_once(card):
    orders, _ = card
    cb = make_callback()
    await owner.on_owner_page(cb, owner.OwnerPageCb(order_id=999), orders, frozenset({1}))
    cb.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    cb.message.edit_text.assert_not_awaited()
