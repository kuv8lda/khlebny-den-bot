"""Оформление заказа (FSM) и раздел «Мои заказы»."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import cart as cart_logic
from . import texts
from .catalog import get_item
from .handlers import CheckoutCb, load_cart, reset_fsm
from .orders import Order, OrderItem, OrdersRepo

log = logging.getLogger(__name__)

# Порядок регистрации важен: сначала «Мои заказы», потом шаги FSM с вводом текста,
# иначе текст кнопки попадёт в поле «имя».
router = Router()

NAME_MAX = 40
COMMENT_MAX = 200
LIST_LIMIT = 5


class Checkout(StatesGroup):
    name = State()
    comment = State()
    confirm = State()


class OrderCb(CallbackData, prefix="v"):
    order_id: int


def snapshot_cart(cart: cart_logic.Cart) -> list[OrderItem]:
    """Позиции корзины с названием и ценой на текущий момент."""
    items: list[OrderItem] = []
    for key, qty in cart.items():
        item = get_item(key)
        if item is not None:
            items.append(OrderItem(item["id"], item["name"], item["price_cents"], qty))
    return items


def format_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M") + " UTC"


def _item_lines(items: Sequence[OrderItem]) -> list[str]:
    return [
        texts.CART_LINE.format(
            name=i.name, qty=i.qty, sum=cart_logic.format_rub(i.price_cents * i.qty)
        )
        for i in items
    ]


def _total(items: Sequence[OrderItem]) -> str:
    return cart_logic.format_rub(sum(i.price_cents * i.qty for i in items))


def render_confirm(
    items: Sequence[OrderItem], name: str, comment: str
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join(
        [
            texts.CHECKOUT_CONFIRM_TITLE,
            "",
            *_item_lines(items),
            "",
            texts.CART_TOTAL.format(total=_total(items)),
            texts.CHECKOUT_NAME_LINE.format(name=name),
            texts.CHECKOUT_COMMENT_LINE.format(comment=comment or texts.NO_COMMENT),
            "",
            texts.DEMO_NOTE,
        ]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_CONFIRM, callback_data=CheckoutCb(action="confirm").pack()
                ),
                InlineKeyboardButton(
                    text=texts.BTN_CANCEL, callback_data=CheckoutCb(action="cancel").pack()
                ),
            ]
        ]
    )
    return text, markup


def render_order(order: Order) -> str:
    lines = [
        texts.ORDER_TITLE.format(
            id=order.id, status=texts.STATUS_NAMES.get(order.status, order.status)
        ),
        texts.ORDER_DATE_LINE.format(date=format_dt(order.created_at)),
        texts.CHECKOUT_NAME_LINE.format(name=order.name),
        texts.CHECKOUT_COMMENT_LINE.format(comment=order.comment or texts.NO_COMMENT),
        "",
        *_item_lines(order.items),
        "",
        texts.CART_TOTAL.format(total=cart_logic.format_rub(order.total_cents)),
        "",
        texts.DEMO_NOTE,
    ]
    return "\n".join(lines)


def _skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_SKIP, callback_data=CheckoutCb(action="skip").pack()
                )
            ]
        ]
    )


async def _edit(callback: CallbackQuery, text: str) -> None:
    """Заменить текст сообщения с кнопками (кнопки при этом убираются)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text)
        except TelegramBadRequest:
            pass  # "message is not modified"


# --- Мои заказы -------------------------------------------------------------


@router.message(F.text == texts.BTN_ORDERS)
async def cmd_orders(message: Message, orders: OrdersRepo) -> None:
    if message.from_user is None:
        return
    found = await orders.list_for_user(message.from_user.id, LIST_LIMIT)
    if not found:
        await message.answer(texts.ORDERS_EMPTY)
        return
    lines = [
        texts.ORDER_LINE.format(
            id=o.id,
            date=format_dt(o.created_at),
            total=cart_logic.format_rub(o.total_cents),
            status=texts.STATUS_NAMES.get(o.status, o.status),
        )
        for o in found
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_ORDER.format(id=o.id),
                    callback_data=OrderCb(order_id=o.id).pack(),
                )
            ]
            for o in found
        ]
    )
    await message.answer("\n".join([texts.ORDERS_TITLE, "", *lines]), reply_markup=markup)


@router.callback_query(OrderCb.filter())
async def on_order_open(
    callback: CallbackQuery, callback_data: OrderCb, orders: OrdersRepo
) -> None:
    order = await orders.get(callback_data.order_id, callback.from_user.id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.answer(render_order(order))
    await callback.answer()


# --- Оформление -------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "start"))
async def on_checkout_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not snapshot_cart(await load_cart(state)):
        if isinstance(callback.message, Message):
            await callback.message.answer(texts.CART_EMPTY)
        await callback.answer()
        return
    # Данные раньше состояния: состояние без данных появиться не может.
    await state.update_data(checkout={"token": uuid.uuid4().hex})
    await state.set_state(Checkout.name)
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.CHECKOUT_ASK_NAME.format(max=NAME_MAX))
    await callback.answer()


@router.message(Checkout.name)
async def on_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        await message.answer(texts.CHECKOUT_NAME_INVALID.format(max=NAME_MAX))
        return
    data = await state.get_data()
    await state.update_data(checkout={**(data.get("checkout") or {}), "name": name})
    await state.set_state(Checkout.comment)
    await message.answer(
        texts.CHECKOUT_ASK_COMMENT.format(max=COMMENT_MAX), reply_markup=_skip_keyboard()
    )


async def _to_confirm(message: Message, state: FSMContext, comment: str) -> None:
    data = await state.get_data()
    checkout = data.get("checkout") or {}
    items = snapshot_cart(dict(data.get("cart", {})))
    if not items or "name" not in checkout:
        await reset_fsm(state)
        await message.answer(texts.CART_EMPTY)
        return
    # Заказ будет ровно тем, что показано на экране подтверждения.
    checkout = {**checkout, "comment": comment, "items": [asdict(i) for i in items]}
    await state.update_data(checkout=checkout)
    await state.set_state(Checkout.confirm)
    text, markup = render_confirm(items, checkout["name"], comment)
    await message.answer(text, reply_markup=markup)


@router.message(Checkout.comment)
async def on_comment(message: Message, state: FSMContext) -> None:
    comment = (message.text or "").strip()
    if not 1 <= len(comment) <= COMMENT_MAX:
        await message.answer(texts.CHECKOUT_COMMENT_INVALID.format(max=COMMENT_MAX))
        return
    await _to_confirm(message, state, comment)


@router.callback_query(CheckoutCb.filter(F.action == "skip"), Checkout.comment)
async def on_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _to_confirm(callback.message, state, "")
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "confirm"), Checkout.confirm)
async def on_confirm(callback: CallbackQuery, state: FSMContext, orders: OrdersRepo) -> None:
    checkout = (await state.get_data()).get("checkout") or {}
    items = [OrderItem(**d) for d in checkout.get("items", [])]
    name, token = checkout.get("name"), checkout.get("token")
    if not (items and name and token):
        await reset_fsm(state)
        await callback.answer(texts.CHECKOUT_STALE, show_alert=True)
        return
    # Повтор с тем же token вернёт тот же заказ, дубля не будет.
    order, created = await orders.create(
        callback.from_user.id, name, checkout.get("comment", ""), items, token
    )
    await reset_fsm(state, clear_cart=True)
    if created:
        log.info("order %s created", order.id)
    template = texts.ORDER_CREATED if created else texts.ORDER_ALREADY
    await _edit(callback, template.format(id=order.id))
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "cancel"), Checkout.confirm)
async def on_cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    await reset_fsm(state)
    await _edit(callback, texts.CANCELLED)
    await callback.answer()


@router.callback_query(CheckoutCb.filter())
async def on_checkout_stale(callback: CallbackQuery) -> None:
    """Кнопки старых сообщений, когда оформление уже не идёт."""
    await callback.answer(texts.CHECKOUT_STALE, show_alert=True)
