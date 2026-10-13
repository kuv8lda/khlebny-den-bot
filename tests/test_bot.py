from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup

from bot import cart as cart_logic
from bot import handlers as h
from bot import texts
from bot.catalog import CATALOG, get_item
from bot.storage import SqliteStorage


def make_state(storage=None) -> FSMContext:
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage or MemoryStorage(), key=key)

def make_message() -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg

def make_callback() -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    cb.message = make_message()
    return cb

async def test_start_shows_buttons_and_demo_note():
    msg = make_message()
    await h.cmd_start(msg)
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.call_args
    assert "демо" in args[0].lower()
    markup = kwargs["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    labels = [b.text for row in markup.keyboard for b in row]
    assert labels == [texts.BTN_MENU, texts.BTN_CART, texts.BTN_ORDERS]


async def test_menu_has_nine_items_with_prices():
    msg = make_message()
    await h.cmd_menu(msg)
    markup = msg.answer.call_args.kwargs["reply_markup"]
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 9
    assert all("₽" in b.text for b in buttons)


async def test_add_item_to_cart():
    state = make_state()
    cb = make_callback()
    await h.on_add(cb, h.MenuCb(item_id=1), state)
    await h.on_add(cb, h.MenuCb(item_id=1), state)
    assert (await state.get_data())["cart"] == {"1": 2}


async def test_add_item_over_max_is_refused():
    state = make_state()
    await state.update_data(cart={"1": 20})
    cb = make_callback()
    await h.on_add(cb, h.MenuCb(item_id=1), state)
    assert (await state.get_data())["cart"] == {"1": 20}
    assert cb.answer.call_args.kwargs.get("show_alert") is True


def test_set_qty_range():
    cart = {"1": 5}
    assert cart_logic.set_qty(cart, 1, 1) and cart["1"] == 1
    assert cart_logic.set_qty(cart, 1, 20) and cart["1"] == 20
    assert not cart_logic.set_qty(cart, 1, 0)
    assert not cart_logic.set_qty(cart, 1, 21)
    assert not cart_logic.set_qty(cart, 2, 3)  # позиции нет в корзине
    assert cart["1"] == 20


async def test_inc_dec_buttons_respect_range():
    state = make_state()
    await state.update_data(cart={"1": 1, "2": 20})
    cb = make_callback()
    await h.on_cart_action(cb, h.CartCb(action="dec", item_id=1), state)
    await h.on_cart_action(cb, h.CartCb(action="inc", item_id=2), state)
    assert (await state.get_data())["cart"] == {"1": 1, "2": 20}
    await h.on_cart_action(cb, h.CartCb(action="inc", item_id=1), state)
    assert (await state.get_data())["cart"]["1"] == 2


async def test_remove_and_clear():
    state = make_state()
    await state.update_data(cart={"1": 2, "2": 1})
    cb = make_callback()
    await h.on_cart_action(cb, h.CartCb(action="del", item_id=1), state)
    assert (await state.get_data())["cart"] == {"2": 1}
    await h.on_cart_action(cb, h.CartCb(action="clear"), state)
    assert (await state.get_data())["cart"] == {}
    assert cb.message.edit_text.call_args.args[0] == texts.CART_EMPTY


def test_total_in_cents_and_rub_format():
    a, b = CATALOG[0], CATALOG[1]
    cart = {str(a["id"]): 2, str(b["id"]): 3}
    assert cart_logic.total_cents(cart) == a["price_cents"] * 2 + b["price_cents"] * 3
    assert cart_logic.format_rub(115000) == "1 150 ₽"
    assert cart_logic.format_rub(115050) == "1 150,50 ₽"
    assert cart_logic.format_rub(0) == "0 ₽"
    assert get_item(a["id"]) is a


async def test_cart_text_shows_total():
    state = make_state()
    await state.update_data(cart={"1": 2})
    msg = make_message()
    await h.cmd_cart(msg, state)
    text = msg.answer.call_args.args[0]
    assert cart_logic.format_rub(CATALOG[0]["price_cents"] * 2) in text


async def test_cancel_clears_state_keeps_cart():
    state = make_state()
    await state.set_state("some:state")
    await state.update_data(cart={"1": 1})
    await h.cmd_cancel(make_message(), state)
    assert await state.get_state() is None
    assert (await state.get_data())["cart"] == {"1": 1}


async def test_fsm_survives_restart(tmp_path):
    db = tmp_path / "fsm.sqlite3"
    storage1 = SqliteStorage(db)
    await h.on_add(make_callback(), h.MenuCb(item_id=3), make_state(storage1))
    await storage1.close()  # «перезапуск»

    storage2 = SqliteStorage(db)
    assert (await make_state(storage2).get_data())["cart"] == {"3": 1}
    await storage2.close()
