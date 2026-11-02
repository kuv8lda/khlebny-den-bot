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
from .ui import page_slice

router = Router()


class MenuCb(CallbackData, prefix="m2"):
    item_id: int


class MenuPageCb(CallbackData, prefix="mp"):
    page: int


class CartCb(CallbackData, prefix="c2"):
    action: str  # inc | dec | del | clear | noop
    item_id: int = 0


class CheckoutCb(CallbackData, prefix="o2"):
    action: str  # start | skip | confirm | cancel
    token: str = ""


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


def menu_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    items, page, pages = page_slice(CATALOG, page, 5)
    rows = [
        [
            InlineKeyboardButton(
                text=texts.MENU_BUTTON.format(
                    name=item["name"], price=cart_logic.format_rub(item["price_cents"])
                ),
                callback_data=MenuCb(item_id=item["id"]).pack(),
            )
        ]
        for item in items
    ]
    nav = []
    for label, target in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1)):
        if 0 <= target < pages:
            nav.append(
                InlineKeyboardButton(text=label, callback_data=MenuPageCb(page=target).pack())
            )
    if nav:
        rows.append(nav)
    rows.append([_btn(texts.BTN_OPEN_CART, "open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_text(page: int = 0) -> str:
    if not CATALOG:
        return texts.MENU_EMPTY
    _, page, pages = page_slice(CATALOG, page, 5)
    return texts.MENU_TITLE + "\n" + texts.PAGE.format(page=page + 1, pages=pages)


def _btn(text: str, action: str, item_id: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=CartCb(action=action, item_id=item_id).pack()
    )


def render_cart(cart: cart_logic.Cart, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    entries = [(key, qty) for key, qty in cart.items() if get_item(key) is not None]
    visible, page, pages = page_slice(entries, page, 2)
    for key, qty in visible:
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
        return texts.CART_EMPTY, InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.BTN_BROWSE, callback_data=MenuPageCb(page=0).pack()
                    )
                ]
            ]
        )
    total = cart_logic.format_rub(cart_logic.total_cents(cart))
    title = texts.CART_TITLE
    if pages > 1:
        title += " · " + texts.PAGE.format(page=page + 1, pages=pages)
    text = "\n".join([title, *lines, texts.CART_TOTAL.format(total=total)])
    nav = []
    for label, target in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1)):
        if 0 <= target < pages:
            nav.append(_btn(label, "page", target))
    if nav:
        rows.append(nav)
    rows.append([_btn(texts.BTN_CLEAR, "clear")])
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.BTN_CHECKOUT, callback_data=CheckoutCb(action="start").pack()
            )
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def load_cart(state: FSMContext) -> cart_logic.Cart:
    data = await state.get_data()
    return dict(data.get("cart", {}))


async def reset_fsm(state: FSMContext, *, clear_cart: bool = False) -> None:
    """Сбросить состояние и данные оформления. Корзина остаётся, если не просили очистить."""
    data = await state.get_data()
    data.pop("checkout", None)
    if clear_cart:
        data["cart"] = {}
    await state.set_state(None)
    await state.set_data(data)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START, reply_markup=main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    # Сбрасываем состояние и данные оформления, корзину сохраняем. См. DECISIONS.md.
    await reset_fsm(state)
    await message.answer(texts.CANCELLED, reply_markup=main_keyboard())


@router.message(Command("menu"))
@router.message(F.text == texts.BTN_MENU)
async def cmd_menu(message: Message) -> None:
    await message.answer(menu_text(), reply_markup=menu_keyboard())


@router.message(Command("cart"))
@router.message(F.text == texts.BTN_CART)
async def cmd_cart(message: Message, state: FSMContext) -> None:
    await state.update_data(cart_page=0)
    text, markup = render_cart(await load_cart(state))
    await message.answer(text, reply_markup=markup)


@router.callback_query(MenuPageCb.filter())
async def on_menu_page(callback: CallbackQuery, callback_data: MenuPageCb) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                menu_text(callback_data.page), reply_markup=menu_keyboard(callback_data.page)
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise


@router.callback_query(MenuCb.filter())
async def on_add(callback: CallbackQuery, callback_data: MenuCb, state: FSMContext) -> None:
    item = get_item(callback_data.item_id)
    if item is None:
        await callback.answer(texts.UNKNOWN_ITEM, show_alert=True)
        return
    cart = await load_cart(state)
    if not cart_logic.add_item(cart, item["id"]):
        await callback.answer(texts.QTY_MAX.format(max=cart_logic.MAX_QTY), show_alert=True)
        return
    await state.update_data(cart=cart)
    await callback.answer(texts.ADDED.format(name=item["name"]))


@router.callback_query(CartCb.filter())
async def on_cart_action(callback: CallbackQuery, callback_data: CartCb, state: FSMContext) -> None:
    action = callback_data.action
    if action == "noop":
        await callback.answer()
        return
    if action == "open":
        await callback.answer()
        if isinstance(callback.message, Message):
            text, markup = render_cart(await load_cart(state))
            await callback.message.answer(text, reply_markup=markup)
        return
    cart = await load_cart(state)
    page = (await state.get_data()).get("cart_page", 0)
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
    elif action == "page":
        page = item_id
    await state.update_data(cart=cart, cart_page=page)
    await callback.answer()

    if isinstance(callback.message, Message):
        text, markup = render_cart(cart, page)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass  # "message is not modified"
