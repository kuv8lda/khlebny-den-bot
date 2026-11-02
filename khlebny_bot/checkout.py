"""Оформление заказа (FSM) и раздел «Мои заказы»."""

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from . import cart as cart_logic
from . import texts
from .catalog import get_item
from .handlers import CheckoutCb, load_cart, reset_fsm
from .orders import Order, OrderItem, OrderRateLimitError, OrdersRepo
from .panel import OrderItemPayload, OrderPayload, PanelClient, PanelError
from .ui import one_line, page_slice

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


class RetryOrderCb(CallbackData, prefix="retry"):
    order_id: int


class OrderCb(CallbackData, prefix="v"):
    order_id: int


class CheckoutViewCb(CallbackData, prefix="cv"):
    token: str
    page: int = -1  # -1: подтверждение, остальные: страницы состава


class OrderViewCb(CallbackData, prefix="ov"):
    order_id: int
    page: int = 0


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
    items: Sequence[OrderItem], name: str, comment: str, token: str
) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join(
        [
            texts.CHECKOUT_CONFIRM_TITLE,
            texts.CHECKOUT_NAME_LINE.format(name=one_line(name))
            + " · "
            + texts.CART_TOTAL.format(total=_total(items)),
            texts.CHECKOUT_COMMENT_LINE.format(comment=one_line(comment) or texts.NO_COMMENT),
            texts.DEMO_NOTE,
        ]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_ITEMS, callback_data=CheckoutViewCb(token=token, page=0).pack()
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.BTN_CONFIRM,
                    callback_data=CheckoutCb(action="confirm", token=token).pack(),
                ),
                InlineKeyboardButton(
                    text=texts.BTN_CANCEL,
                    callback_data=CheckoutCb(action="cancel", token=token).pack(),
                ),
            ],
        ]
    )
    return text, markup


def _order_lines(order: Order) -> list[str]:
    return [
        *_item_lines(order.items),
        texts.CHECKOUT_NAME_LINE.format(name=one_line(order.name)),
        texts.CHECKOUT_COMMENT_LINE.format(comment=one_line(order.comment) or texts.NO_COMMENT),
        texts.ORDER_DATE_LINE.format(date=format_dt(order.created_at)),
        texts.DEMO_NOTE,
    ]


def render_order(order: Order, page: int = 0) -> str:
    visible, page, pages = page_slice(_order_lines(order), page, 2)
    title = texts.ORDER_TITLE.format(
        id=order.id, status=texts.STATUS_NAMES.get(order.status, order.status)
    )
    return "\n".join(
        [
            title + f" · {page + 1}/{pages}",
            *visible,
            texts.CART_TOTAL.format(total=cart_logic.format_rub(order.total_cents)),
        ]
    )


def _order_keyboard(order: Order, page: int = 0) -> InlineKeyboardMarkup:
    _, page, pages = page_slice(_order_lines(order), page, 2)
    nav = [
        InlineKeyboardButton(
            text=label, callback_data=OrderViewCb(order_id=order.id, page=n).pack()
        )
        for label, n in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1))
        if 0 <= n < pages
    ]
    rows = [nav] if nav else []
    retry = _retry_keyboard(order)
    if retry is not None:
        rows.extend(retry.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _as_int(value: object) -> int | None:
    """id заказа из ответа панели: int или строка из цифр, иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_CANCEL,
                    callback_data=CheckoutCb(action="cancel", token=token).pack(),
                )
            ]
        ]
    )


def _skip_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_SKIP, callback_data=CheckoutCb(action="skip", token=token).pack()
                )
            ],
            *_cancel_keyboard(token).inline_keyboard,
        ]
    )


async def _edit(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise


async def _current_checkout(callback: CallbackQuery, state: FSMContext, action: str) -> bool:
    checkout = (await state.get_data()).get("checkout") or {}
    try:
        button = CheckoutCb.unpack(callback.data or "")
    except (ValueError, TypeError):
        button = None
    if (
        not button
        or button.action != action
        or not button.token
        or (button.token != checkout.get("token"))
    ):
        await callback.answer(texts.CHECKOUT_STALE, show_alert=True)
        return False
    return True


def _retry_keyboard(order: Order, *, pending: bool = False) -> InlineKeyboardMarkup | None:
    if not (pending or order.pending_panel_sync):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_RETRY_ORDER,
                    callback_data=RetryOrderCb(order_id=order.id).pack(),
                )
            ]
        ]
    )


async def _send_order(
    order: Order, token: str, orders: OrdersRepo, panel_client: PanelClient
) -> None:
    payload = OrderPayload(
        customer_name=order.name,
        items=[OrderItemPayload(product_id=i.item_id, quantity=i.qty) for i in order.items],
        telegram_user_id=order.user_id,
    )
    result = await panel_client.create_order(payload, idempotency_key=token)
    log.info(
        "order %s sent to panel: panel_id=%s duplicate=%s",
        order.id,
        result.panel_order_id,
        result.duplicate,
    )
    panel_id = _as_int(result.panel_order_id)
    if panel_id is not None:
        await orders.set_panel_order_id(order.id, panel_id)
    await orders.mark_panel_synced(order.id)


@router.callback_query(RetryOrderCb.filter())
async def on_order_retry(
    callback: CallbackQuery,
    callback_data: RetryOrderCb,
    orders: OrdersRepo,
    panel_client: PanelClient | None = None,
) -> None:
    order = await orders.get(callback_data.order_id, callback.from_user.id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if not order.pending_panel_sync:
        await callback.answer()
        await _edit(callback, texts.ORDER_ALREADY.format(id=order.id))
        return
    token = await orders.checkout_token(order.id, callback.from_user.id)
    if panel_client is None or token is None:
        await callback.answer(texts.PANEL_RETRY_UNAVAILABLE, show_alert=True)
        return
    await callback.answer()
    try:
        async with (
            ChatActionSender.typing(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                message_thread_id=callback.message.message_thread_id,
            )
            if isinstance(callback.message, Message)
            else nullcontext()
        ):
            await _send_order(order, token, orders, panel_client)
    except PanelError:
        await _edit(callback, texts.PANEL_RETRY_UNAVAILABLE, _retry_keyboard(order))
        return
    await _edit(callback, texts.ORDER_CREATED.format(id=order.id))


# Мои заказы


@router.message(Command("orders"))
@router.message(F.text == texts.BTN_ORDERS)
async def cmd_orders(message: Message, orders: OrdersRepo) -> None:
    if message.from_user is None:
        return
    found = await orders.list_for_user(message.from_user.id, LIST_LIMIT)
    if not found:
        await message.answer(texts.ORDERS_EMPTY)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        texts.BTN_ORDER.format(id=o.id)
                        + " · "
                        + cart_logic.format_rub(o.total_cents)
                        + " · "
                        + texts.STATUS_NAMES.get(o.status, o.status)
                    ),
                    callback_data=OrderCb(order_id=o.id).pack(),
                )
            ]
            for o in found
        ]
    )
    await message.answer(texts.ORDERS_TITLE, reply_markup=markup)


@router.callback_query(OrderCb.filter())
async def on_order_open(
    callback: CallbackQuery, callback_data: OrderCb, orders: OrdersRepo
) -> None:
    order = await orders.get(callback_data.order_id, callback.from_user.id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(render_order(order), reply_markup=_order_keyboard(order))


@router.callback_query(OrderViewCb.filter())
async def on_order_page(
    callback: CallbackQuery, callback_data: OrderViewCb, orders: OrdersRepo
) -> None:
    order = await orders.get(callback_data.order_id, callback.from_user.id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _edit(
        callback,
        render_order(order, callback_data.page),
        _order_keyboard(order, callback_data.page),
    )


# Оформление


@router.callback_query(CheckoutCb.filter(F.action == "start"))
async def on_checkout_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not snapshot_cart(await load_cart(state)):
        if isinstance(callback.message, Message):
            await callback.message.answer(texts.CART_EMPTY)
        return
    # Данные раньше состояния: состояние без данных появиться не может.
    await state.update_data(checkout={"token": uuid.uuid4().hex})
    await state.set_state(Checkout.name)
    if isinstance(callback.message, Message):
        token = (await state.get_data())["checkout"]["token"]
        await callback.message.answer(
            texts.CHECKOUT_ASK_NAME.format(max=NAME_MAX), reply_markup=_cancel_keyboard(token)
        )


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
        texts.CHECKOUT_ASK_COMMENT.format(max=COMMENT_MAX),
        reply_markup=_skip_keyboard((data.get("checkout") or {})["token"]),
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
    text, markup = render_confirm(items, checkout["name"], comment, checkout["token"])
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
    if not await _current_checkout(callback, state, "skip"):
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _to_confirm(callback.message, state, "")


@router.callback_query(CheckoutCb.filter(F.action == "confirm"), Checkout.confirm)
async def on_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    orders: OrdersRepo,
    panel_client: PanelClient | None = None,
    owner_notifier: Callable[[Order], Awaitable[None]] | None = None,
) -> None:
    if not await _current_checkout(callback, state, "confirm"):
        return
    checkout = (await state.get_data()).get("checkout") or {}
    items = [OrderItem(**d) for d in checkout.get("items", [])]
    name, token = checkout.get("name"), checkout.get("token")
    if not (items and name and token):
        await reset_fsm(state)
        await callback.answer(texts.CHECKOUT_STALE, show_alert=True)
        return
    # Убираем индикатор кнопки до записи в БД и сетевого запроса.
    await callback.answer()
    try:
        order, created = await orders.create(
            callback.from_user.id,
            name,
            checkout.get("comment", ""),
            items,
            token,
            pending_panel_sync=panel_client is not None,
            enforce_rate_limit=True,
        )
    except OrderRateLimitError:
        if isinstance(callback.message, Message):
            await callback.message.answer(texts.ORDER_RATE_LIMIT)
        return
    if created:
        log.info("order %s created", order.id)

    if (created or order.pending_panel_sync) and panel_client is not None:
        try:
            async with (
                ChatActionSender.typing(
                    bot=callback.bot,
                    chat_id=callback.message.chat.id,
                    message_thread_id=callback.message.message_thread_id,
                )
                if isinstance(callback.message, Message)
                else nullcontext()
            ):
                await _send_order(order, token, orders, panel_client)
        except PanelError as exc:
            log.warning("panel unavailable for order %s, flagged for retry: %s", order.id, exc)
            await orders.mark_panel_unsynced(order.id)
            await reset_fsm(state, clear_cart=True)
            await _edit(
                callback,
                texts.PANEL_ORDER_UNAVAILABLE.format(id=order.id),
                _retry_keyboard(order, pending=True),
            )
            if created and owner_notifier is not None:
                try:
                    await owner_notifier(await orders.get_by_id(order.id) or order)
                except Exception:
                    log.exception("owner notification failed for order %s", order.id)
            return

    await reset_fsm(state, clear_cart=True)
    template = texts.ORDER_CREATED if created else texts.ORDER_ALREADY
    await _edit(callback, template.format(id=order.id))

    # Сбой уведомления владельца не отменяет сохранённый заказ.
    if created and owner_notifier is not None:
        try:
            await owner_notifier(await orders.get_by_id(order.id) or order)
        except Exception:
            log.exception("owner notification failed for order %s", order.id)


@router.callback_query(
    CheckoutCb.filter(F.action == "cancel"),
    StateFilter(Checkout.name, Checkout.comment, Checkout.confirm),
)
async def on_cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _current_checkout(callback, state, "cancel"):
        return
    await callback.answer()
    await reset_fsm(state)
    await _edit(callback, texts.CANCELLED)


@router.callback_query(CheckoutViewCb.filter(), Checkout.confirm)
async def on_checkout_view(
    callback: CallbackQuery, callback_data: CheckoutViewCb, state: FSMContext
) -> None:
    checkout = (await state.get_data()).get("checkout") or {}
    if not callback_data.token or callback_data.token != checkout.get("token"):
        await callback.answer(texts.CHECKOUT_STALE, show_alert=True)
        return
    await callback.answer()
    items = [OrderItem(**d) for d in checkout.get("items", [])]
    if callback_data.page < 0:
        text, markup = render_confirm(
            items, checkout["name"], checkout["comment"], callback_data.token
        )
    else:
        visible, page, pages = page_slice(_item_lines(items), callback_data.page, 2)
        text = "\n".join(
            [
                f"📋 Состав заказа · {page + 1}/{pages}",
                *visible,
                texts.CART_TOTAL.format(total=_total(items)),
            ]
        )
        nav = [
            InlineKeyboardButton(
                text=label, callback_data=CheckoutViewCb(token=callback_data.token, page=n).pack()
            )
            for label, n in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1))
            if 0 <= n < pages
        ]
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                *([nav] if nav else []),
                [
                    InlineKeyboardButton(
                        text=texts.BTN_BACK_CONFIRM,
                        callback_data=CheckoutViewCb(token=callback_data.token).pack(),
                    )
                ],
                *_cancel_keyboard(callback_data.token).inline_keyboard,
            ]
        )
    await _edit(callback, text, markup)


@router.callback_query(CheckoutViewCb.filter())
async def on_checkout_view_stale(callback: CallbackQuery) -> None:
    await callback.answer(texts.CHECKOUT_STALE, show_alert=True)


@router.callback_query(CheckoutCb.filter())
async def on_checkout_stale(callback: CallbackQuery) -> None:
    """Кнопки старых сообщений, когда оформление уже не идёт."""
    await callback.answer(texts.CHECKOUT_STALE, show_alert=True)


@router.callback_query(F.data.startswith("o2:"))
async def on_legacy_checkout_stale(callback: CallbackQuery) -> None:
    await on_checkout_stale(callback)
