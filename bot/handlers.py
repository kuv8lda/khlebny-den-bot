from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from . import cart as cart_logic
from . import texts
from .catalog import CATALOG, get_item

router = Router()


class MenuCb(CallbackData, prefix="m"):
    item_id: int


class CartCb(CallbackData, prefix="c"):
    action: str  # inc | dec | del | clear | noop
    item_id: int = 0


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=texts.BTN_MENU),
                KeyboardButton(text=texts.BTN_CART),
                KeyboardButton(text=texts.BTN_ORDERS),
            ]
        ],
        resize_keyboard=True,
    )


def menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=texts.MENU_BUTTON.format(
                    name=item["name"], price=cart_logic.format_rub(item["price_cents"])
                ),
                callback_data=MenuCb(item_id=item["id"]).pack(),
            )
        ]
        for item in CATALOG
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text: str, action: str, item_id: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=CartCb(action=action, item_id=item_id).pack()
    )


def render_cart(cart: cart_logic.Cart) -> tuple[str, InlineKeyboardMarkup | None]:
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for key, qty in cart.items():
        item = get_item(key)
        if item is None:
            continue
        item_id = item["id"]
        lines.append(
            texts.CART_LINE.format(
                name=item["name"], qty=qty, sum=cart_logic.format_rub(item["price_cents"] * qty)
            )
        )
        rows.append(
            [
                _btn(texts.BTN_DEC, "dec", item_id),
                _btn(texts.BTN_QTY.format(name=item["name"], qty=qty), "noop", item_id),
                _btn(texts.BTN_INC, "inc", item_id),
                _btn(texts.BTN_REMOVE, "del", item_id),
            ]
        )
    if not lines:
        return texts.CART_EMPTY, None
    total = cart_logic.format_rub(cart_logic.total_cents(cart))
    text = "\n".join([texts.CART_TITLE, "", *lines, "", texts.CART_TOTAL.format(total=total)])
    rows.append([_btn(texts.BTN_CLEAR, "clear")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_cart(state: FSMContext) -> cart_logic.Cart:
    data = await state.get_data()
    return dict(data.get("cart", {}))


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START, reply_markup=main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    # Сбрасываем состояние, данные (корзину) сохраняем. См. DECISIONS.md.
    await state.set_state(None)
    await message.answer(texts.CANCELLED, reply_markup=main_keyboard())


@router.message(Command("menu"))
@router.message(F.text == texts.BTN_MENU)
async def cmd_menu(message: Message) -> None:
    await message.answer(texts.MENU_TITLE, reply_markup=menu_keyboard())


@router.message(Command("cart"))
@router.message(F.text == texts.BTN_CART)
async def cmd_cart(message: Message, state: FSMContext) -> None:
    text, markup = render_cart(await _load_cart(state))
    await message.answer(text, reply_markup=markup)


@router.message(F.text == texts.BTN_ORDERS)
async def cmd_orders(message: Message) -> None:
    await message.answer(texts.ORDERS_STUB)


@router.callback_query(MenuCb.filter())
async def on_add(callback: CallbackQuery, callback_data: MenuCb, state: FSMContext) -> None:
    item = get_item(callback_data.item_id)
    if item is None:
        await callback.answer(texts.UNKNOWN_ITEM, show_alert=True)
        return
    cart = await _load_cart(state)
    if not cart_logic.add_item(cart, item["id"]):
        await callback.answer(texts.QTY_MAX.format(max=cart_logic.MAX_QTY), show_alert=True)
        return
    await state.update_data(cart=cart)
    await callback.answer(texts.ADDED.format(name=item["name"]))


@router.callback_query(CartCb.filter())
async def on_cart_action(
    callback: CallbackQuery, callback_data: CartCb, state: FSMContext
) -> None:
    action = callback_data.action
    if action == "noop":
        await callback.answer()
        return
    cart = await _load_cart(state)
    item_id = callback_data.item_id
    if action in ("inc", "dec"):
        new_qty = cart.get(str(item_id), 0) + (1 if action == "inc" else -1)
        if not cart_logic.set_qty(cart, item_id, new_qty):
            alert = (
                texts.QTY_MAX.format(max=cart_logic.MAX_QTY)
                if new_qty > cart_logic.MAX_QTY
                else texts.QTY_MIN.format(min=cart_logic.MIN_QTY)
            )
            await callback.answer(alert, show_alert=True)
            return
    elif action == "del":
        cart_logic.remove_item(cart, item_id)
    elif action == "clear":
        cart_logic.clear(cart)
    await state.update_data(cart=cart)

    if isinstance(callback.message, Message):
        text, markup = render_cart(cart)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass  # "message is not modified"
    await callback.answer()
