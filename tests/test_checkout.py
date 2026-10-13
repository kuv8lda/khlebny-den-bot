import pytest
from helpers import make_callback, make_message, make_state

from khlebny_bot import cart as cart_logic
from khlebny_bot import checkout as co
from khlebny_bot import handlers as h
from khlebny_bot import texts
from khlebny_bot.catalog import CATALOG
from khlebny_bot.checkout import Checkout
from khlebny_bot.orders import OrderItem, OrdersRepo
from khlebny_bot.storage import SqliteStorage

CART = {"1": 2, "3": 1}


@pytest.fixture
async def orders(tmp_path):
    repo = OrdersRepo(tmp_path / "orders.sqlite3")
    yield repo
    await repo.close()


async def go_to(step, state, cart=None):
    """Довести оформление до шага: name | comment | confirm."""
    await state.update_data(cart=dict(cart or CART))
    await co.on_checkout_start(make_callback(), state)
    if step == "name":
        return
    await co.on_name(make_message("Аня"), state)
    if step == "comment":
        return
    await co.on_comment(make_message("Без глютена"), state)


async def user_orders(orders, user_id=1):
    return await orders.list_for_user(user_id, 100)


def test_cart_has_checkout_button_under_clear():
    _, markup = h.render_cart(dict(CART))
    last, before = markup.inline_keyboard[-1][0], markup.inline_keyboard[-2][0]
    assert before.text == texts.BTN_CLEAR
    assert last.text == texts.BTN_CHECKOUT
    assert h.CheckoutCb.unpack(last.callback_data).action == "start"


@pytest.mark.parametrize("cart", [{}, {"999": 1}])
async def test_checkout_unavailable_for_empty_cart(cart):
    state = make_state()
    await state.update_data(cart=cart)
    cb = make_callback()
    await co.on_checkout_start(cb, state)
    assert await state.get_state() is None
    cb.message.answer.assert_awaited_once_with(texts.CART_EMPTY)


async def test_full_path_creates_order_and_clears_cart(orders):
    state = make_state()
    await go_to("name", state)
    assert await state.get_state() == Checkout.name.state

    msg = make_message("Аня")
    await co.on_name(msg, state)
    assert await state.get_state() == Checkout.comment.state
    skip = msg.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert skip.text == texts.BTN_SKIP

    msg = make_message("Без глютена")
    await co.on_comment(msg, state)
    assert await state.get_state() == Checkout.confirm.state
    screen = msg.answer.call_args.args[0]
    expected_total = cart_logic.format_rub(cart_logic.total_cents(CART))
    for part in ("Аня", "Без глютена", expected_total, "демо"):
        assert part in screen

    cb = make_callback()
    await co.on_confirm(cb, state, orders)
    (order,) = await user_orders(orders)
    assert order.status == "new"
    assert order.name == "Аня" and order.comment == "Без глютена"
    assert order.total_cents == cart_logic.total_cents(CART)
    assert f"№{order.id}" in cb.message.edit_text.call_args.args[0]
    data = await state.get_data()
    assert data["cart"] == {} and "checkout" not in data
    assert await state.get_state() is None


async def test_skip_comment(orders):
    state = make_state()
    await go_to("comment", state)
    cb = make_callback()
    await co.on_skip(cb, state)
    assert await state.get_state() == Checkout.confirm.state
    cb.message.edit_reply_markup.assert_awaited_once()
    await co.on_confirm(make_callback(), state, orders)
    (order,) = await user_orders(orders)
    assert order.comment == ""


@pytest.mark.parametrize("bad", [None, "", "   ", "x" * 41])
async def test_invalid_name_keeps_state(bad):
    state = make_state()
    await go_to("name", state)
    before = await state.get_data()
    msg = make_message(bad)
    await co.on_name(msg, state)
    assert await state.get_state() == Checkout.name.state
    assert await state.get_data() == before
    assert msg.answer.call_args.args[0] == texts.CHECKOUT_NAME_INVALID.format(max=co.NAME_MAX)


@pytest.mark.parametrize("bad", [None, "   ", "x" * 201])
async def test_invalid_comment_keeps_state(bad):
    state = make_state()
    await go_to("comment", state)
    before = await state.get_data()
    msg = make_message(bad)
    await co.on_comment(msg, state)
    assert await state.get_state() == Checkout.comment.state
    assert await state.get_data() == before
    assert msg.answer.call_args.args[0] == texts.CHECKOUT_COMMENT_INVALID.format(
        max=co.COMMENT_MAX
    )


async def test_limits_are_inclusive():
    state = make_state()
    await state.update_data(cart=dict(CART))
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message("x" * 40), state)
    assert await state.get_state() == Checkout.comment.state
    await co.on_comment(make_message("y" * 200), state)
    assert await state.get_state() == Checkout.confirm.state


@pytest.mark.parametrize("step", ["name", "comment", "confirm"])
async def test_cancel_at_every_step(step, orders):
    state = make_state()
    await go_to(step, state)
    await h.cmd_cancel(make_message("/cancel"), state)
    assert await state.get_state() is None
    data = await state.get_data()
    assert data["cart"] == CART and "checkout" not in data
    assert await user_orders(orders) == []


async def test_cancel_button_on_confirm_screen(orders):
    state = make_state()
    await go_to("confirm", state)
    cb = make_callback()
    await co.on_cancel_button(cb, state)
    assert await state.get_state() is None
    assert (await state.get_data())["cart"] == CART
    assert await user_orders(orders) == []


async def test_order_total_matches_cart_total(orders):
    cart = {str(CATALOG[0]["id"]): 3, str(CATALOG[4]["id"]): 2}
    state = make_state()
    await go_to("confirm", state, cart)
    cart_total = cart_logic.total_cents(cart)
    await co.on_confirm(make_callback(), state, orders)
    (order,) = await user_orders(orders)
    assert order.total_cents == cart_total
    full = await orders.get(order.id, 1)
    assert sum(i.price_cents * i.qty for i in full.items) == cart_total


async def test_catalog_change_does_not_touch_saved_order(orders, monkeypatch):
    state = make_state()
    await go_to("confirm", state)
    old_name, old_price = CATALOG[0]["name"], CATALOG[0]["price_cents"]
    await co.on_confirm(make_callback(), state, orders)
    monkeypatch.setitem(CATALOG[0], "name", "Новое название")
    monkeypatch.setitem(CATALOG[0], "price_cents", 1)
    (order,) = await user_orders(orders)
    full = await orders.get(order.id, 1)
    first = next(i for i in full.items if i.item_id == CATALOG[0]["id"])
    assert (first.name, first.price_cents) == (old_name, old_price)
    assert full.total_cents == 14000 * 2 + 11000


async def test_repeated_confirm_does_not_duplicate(orders):
    state = make_state()
    await go_to("confirm", state)
    saved_data, saved_state = await state.get_data(), await state.get_state()
    await co.on_confirm(make_callback(), state, orders)
    (order,) = await user_orders(orders)
    # Повтор: например, ответ Telegram потерялся или нажали дважды.
    await state.set_data(saved_data)
    await state.set_state(saved_state)
    cb = make_callback()
    await co.on_confirm(cb, state, orders)
    assert len(await user_orders(orders)) == 1
    assert cb.message.edit_text.call_args.args[0] == texts.ORDER_ALREADY.format(id=order.id)
    assert (await state.get_data())["cart"] == {}


async def test_confirm_button_outside_checkout_is_stale():
    cb = make_callback()
    await co.on_checkout_stale(cb)
    assert cb.answer.call_args.kwargs.get("show_alert") is True


async def test_my_orders_empty(orders):
    msg = make_message(texts.BTN_ORDERS)
    await co.cmd_orders(msg, orders)
    assert msg.answer.call_args.args[0] == texts.ORDERS_EMPTY


async def make_order(orders, user_id, token):
    items = [OrderItem(1, "Ржаной хлеб на закваске", 14000, 2)]
    return (await orders.create(user_id, "Аня", "", items, token))[0]


async def test_my_orders_lists_latest_five_newest_first(orders):
    made = [await make_order(orders, 1, f"t{n}") for n in range(7)]
    msg = make_message(texts.BTN_ORDERS)
    await co.cmd_orders(msg, orders)
    text = msg.answer.call_args.args[0]
    markup = msg.answer.call_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [co.OrderCb.unpack(b.callback_data).order_id for b in buttons] == [
        o.id for o in reversed(made[2:])
    ]
    assert f"№{made[-1].id}" in text and f"№{made[0].id}" not in text
    assert "280 ₽" in text and "новый" in text


async def test_my_orders_only_own(orders):
    mine = await make_order(orders, 1, "a")
    theirs = await make_order(orders, 2, "b")
    msg = make_message(texts.BTN_ORDERS, user_id=1)
    await co.cmd_orders(msg, orders)
    text = msg.answer.call_args.args[0]
    assert f"№{mine.id}" in text and f"№{theirs.id}" not in text


async def test_open_order_shows_items_and_hides_foreign(orders):
    mine = await make_order(orders, 1, "a")
    theirs = await make_order(orders, 2, "b")
    cb = make_callback(user_id=1)
    await co.on_order_open(cb, co.OrderCb(order_id=mine.id), orders)
    text = cb.message.answer.call_args.args[0]
    assert "Ржаной хлеб на закваске × 2" in text
    cb = make_callback(user_id=1)
    await co.on_order_open(cb, co.OrderCb(order_id=theirs.id), orders)
    cb.message.answer.assert_not_awaited()
    assert cb.answer.call_args.kwargs.get("show_alert") is True


async def test_checkout_state_and_orders_survive_restart(tmp_path):
    db = tmp_path / "app.sqlite3"

    def open_all():
        return SqliteStorage(db), OrdersRepo(db)

    storage, orders = open_all()
    state = make_state(storage)
    await go_to("name", state)
    await co.on_name(make_message("Аня"), state)
    await storage.close()
    await orders.close()  # «перезапуск» посреди оформления

    storage, orders = open_all()
    state = make_state(storage)
    assert await state.get_state() == Checkout.comment.state
    await co.on_skip(make_callback(), state)
    await storage.close()
    await orders.close()

    storage, orders = open_all()
    state = make_state(storage)
    assert await state.get_state() == Checkout.confirm.state
    await co.on_confirm(make_callback(), state, orders)
    await storage.close()
    await orders.close()

    storage, orders = open_all()
    (order,) = await user_orders(orders)
    assert order.total_cents == cart_logic.total_cents(CART)
    assert (await make_state(storage).get_data())["cart"] == {}
    await storage.close()
    await orders.close()
