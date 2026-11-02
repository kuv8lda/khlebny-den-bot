"""Структура экранов, страницы и порядок callback-ответов."""

from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest
from helpers import make_callback, make_checkout_callback, make_state

from khlebny_bot import cart, checkout, handlers, owner, texts
from khlebny_bot.catalog import CATALOG
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.panel import PanelClient, PanelError
from khlebny_bot.ui import page_slice


@pytest.mark.parametrize("page, expected", [(-1, 0), (0, 0), (1, 1), (999, 1)])
def test_page_bounds(page, expected):
    visible, actual, pages = page_slice([1, 2, 3], page, 2)
    assert actual == expected and pages == 2
    assert visible == ([1, 2] if expected == 0 else [3])


def test_menu_pages_expose_entire_catalog_once():
    ids = []
    for page in range((len(CATALOG) + 4) // 5):
        markup = handlers.menu_keyboard(page)
        products = [
            b for row in markup.inline_keyboard for b in row if b.callback_data.startswith("m2:")
        ]
        assert 1 <= len(products) <= 5
        ids.extend(handlers.MenuCb.unpack(b.callback_data).item_id for b in products)
        assert len(handlers.menu_text(page).splitlines()) <= 4
    assert ids == [i["id"] for i in CATALOG]


def test_empty_menu_has_explanation_and_cart(monkeypatch):
    monkeypatch.setattr(handlers, "CATALOG", [])
    assert handlers.menu_text() == texts.MENU_EMPTY
    assert handlers.menu_keyboard().inline_keyboard[0][0].callback_data == "c2:open:0"


def test_cart_pages_keep_full_total_and_limit_lines():
    data = {str(i["id"]): 1 for i in CATALOG}
    seen = []
    for page in range((len(CATALOG) + 1) // 2):
        text, markup = handlers.render_cart(data, page)
        assert len(text.splitlines()) <= 4
        assert cart.format_rub(cart.total_cents(data)) in text
        seen.extend(
            handlers.CartCb.unpack(b.callback_data).item_id
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("c2:inc:")
        )
    assert seen == [i["id"] for i in CATALOG]


async def test_cart_page_clamps_after_removing_last_item():
    state = make_state()
    await state.update_data(cart={"1": 1, "2": 1, "3": 1}, cart_page=1)
    cb = make_callback()
    await handlers.on_cart_action(cb, handlers.CartCb(action="del", item_id=3), state)
    text = cb.message.edit_text.call_args.args[0]
    assert CATALOG[0]["name"] in text
    assert len(text.splitlines()) <= 4
    cb.answer.assert_awaited_once_with()


@pytest.mark.parametrize("screen", ["menu", "cart"])
async def test_page_ack_precedes_edit(screen):
    cb = make_callback()

    async def edit(*args, **kwargs):
        cb.answer.assert_awaited_once_with()

    cb.message.edit_text.side_effect = edit
    if screen == "menu":
        await handlers.on_menu_page(cb, handlers.MenuPageCb(page=1))
    else:
        state = make_state()
        await state.update_data(cart={"1": 1, "2": 1, "3": 1})
        await handlers.on_cart_action(cb, handlers.CartCb(action="page", item_id=1), state)
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once_with()


async def prepared_checkout():
    state = make_state()
    items = checkout.snapshot_cart({str(i["id"]): 1 for i in CATALOG})
    await state.update_data(
        cart={str(i.item_id): i.qty for i in items},
        checkout={
            "token": "test-token",
            "name": "Аня",
            "comment": "Без\nсахара",
            "items": [asdict(i) for i in items],
        },
    )
    await state.set_state(checkout.Checkout.confirm)
    return state, items


async def test_confirmation_summary_and_composition_pages():
    state, items = await prepared_checkout()
    before = await state.get_data()
    text, markup = checkout.render_confirm(items, "Аня", "Без\nсахара", "test-token")
    assert len(text.splitlines()) == 4
    assert "Без сахара" in text and checkout._total(items) in text
    data = checkout.CheckoutViewCb.unpack(markup.inline_keyboard[0][0].callback_data)
    for page in range((len(items) + 1) // 2):
        cb = make_callback()
        data.page = page
        await checkout.on_checkout_view(cb, data, state)
        cb.answer.assert_awaited_once_with()
        assert len(cb.message.edit_text.call_args.args[0].splitlines()) <= 4
    cb = make_callback()
    await checkout.on_checkout_view(cb, checkout.CheckoutViewCb(token="test-token"), state)
    assert cb.message.edit_text.call_args.args[0] == text
    assert await state.get_data() == before
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_old_composition_button_cannot_open_new_checkout():
    state, _ = await prepared_checkout()
    cb = make_callback()
    await checkout.on_checkout_view(cb, checkout.CheckoutViewCb(token="old"), state)
    cb.answer.assert_awaited_once_with(texts.CHECKOUT_STALE, show_alert=True)
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.parametrize(
    "step", [checkout.Checkout.name, checkout.Checkout.comment, checkout.Checkout.confirm]
)
async def test_cancel_button_preserves_cart_at_every_step(step):
    state, _ = await prepared_checkout()
    await state.set_state(step)
    before = (await state.get_data())["cart"]
    cb = await make_checkout_callback(state, "cancel")
    await checkout.on_cancel_button(cb, state)
    cb.answer.assert_awaited_once_with()
    assert await state.get_state() is None
    assert (await state.get_data())["cart"] == before
    assert "checkout" not in await state.get_data()


async def test_order_pages_keep_all_details_and_reject_foreign_user(tmp_path):
    orders = OrdersRepo(tmp_path / "orders.db")
    try:
        order, _ = await orders.create(
            1, "Аня", "Без сахара", checkout.snapshot_cart({"1": 2, "2": 1, "3": 1}), "t"
        )
        _, _, pages = page_slice(checkout._order_lines(order), 0, 2)
        screens = []
        for page in range(pages):
            cb = make_callback()
            await checkout.on_order_page(
                cb, checkout.OrderViewCb(order_id=order.id, page=page), orders
            )
            text = cb.message.edit_text.call_args.args[0]
            assert len(text.splitlines()) <= 4
            screens.append(text)
            cb.answer.assert_awaited_once_with()
        combined = "\n".join(screens)
        for detail in ("Аня", "Без сахара", texts.DEMO_NOTE, *checkout._item_lines(order.items)):
            assert detail in combined
        cb = make_callback(user_id=2)
        await checkout.on_order_page(cb, checkout.OrderViewCb(order_id=order.id), orders)
        cb.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
        cb.message.edit_text.assert_not_awaited()
    finally:
        await orders.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_owner_ack_precedes_panel_and_typing_stops(tmp_path, monkeypatch, failed):
    orders = OrdersRepo(tmp_path / "orders.db")
    try:
        order, _ = await orders.create(1, "Аня", "", [OrderItem(1, "Хлеб", 100, 1)], "t")
        await orders.set_panel_order_id(order.id, 9)
        cb = make_callback()
        sender = MagicMock()
        sender.__aenter__ = AsyncMock()
        sender.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(owner.ChatActionSender, "typing", MagicMock(return_value=sender))
        panel = AsyncMock(spec=PanelClient)

        async def patch(*args, **kwargs):
            cb.answer.assert_awaited_once_with()
            sender.__aenter__.assert_awaited_once()
            sender.__aexit__.assert_not_awaited()
            if failed:
                raise PanelError("down")
            return "paid"

        panel.patch_status.side_effect = patch
        await owner.on_status(
            cb, owner.OwnerCb(order_id=order.id, status="paid"), orders, panel, frozenset({1})
        )
        cb.answer.assert_awaited_once_with()
        sender.__aexit__.assert_awaited_once()
        assert (await orders.get_by_id(order.id)).status == ("new" if failed else "paid")
    finally:
        await orders.close()
