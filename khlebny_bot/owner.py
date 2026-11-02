"""Уведомления и управление заказами для ID из OWNER_TELEGRAM_IDS."""

import logging
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import replace

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from . import cart as cart_logic
from . import texts
from .checkout import _item_lines, format_dt
from .orders import Order, OrdersRepo
from .panel import PanelClient, PanelConflictError, PanelError
from .ui import one_line, page_slice

log = logging.getLogger(__name__)

router = Router()

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("paid", "cancelled"),
    "paid": ("shipped", "cancelled"),
    "shipped": ("completed",),
    "completed": (),
    "cancelled": (),
}


class OwnerCb(CallbackData, prefix="ow"):
    order_id: int  # номер заказа в боте
    status: str  # на какой статус перевести


def effective_status(status: str) -> str:
    """Локальный «new» — это pending в терминах панели."""
    return "pending" if status == "new" else status


def next_statuses(order: Order) -> tuple[str, ...]:
    """Допустимые переходы; без ID панели переходов нет."""
    if order.panel_order_id is None:
        return ()
    return TRANSITIONS.get(effective_status(order.status), ())


class OwnerPageCb(CallbackData, prefix="op"):
    order_id: int
    page: int = 0


def _owner_lines(order: Order) -> list[str]:
    return [
        *([texts.OWNER_NOT_SYNCED] if order.panel_order_id is None else []),
        texts.CHECKOUT_NAME_LINE.format(name=one_line(order.name)),
        texts.CHECKOUT_COMMENT_LINE.format(comment=one_line(order.comment) or texts.NO_COMMENT),
        texts.ORDER_DATE_LINE.format(date=format_dt(order.created_at)),
        *_item_lines(order.items),
    ]


def render_owner_order(order: Order, page: int = 0) -> str:
    status = texts.STATUS_NAMES.get(effective_status(order.status), order.status)
    visible, page, pages = page_slice(_owner_lines(order), page, 2)
    return "\n".join(
        [
            texts.ORDER_TITLE.format(id=order.id, status=status) + f" · {page + 1}/{pages}",
            *visible,
            texts.CART_TOTAL.format(total=cart_logic.format_rub(order.total_cents)),
        ]
    )


def owner_keyboard(order: Order) -> InlineKeyboardMarkup | None:
    targets = next_statuses(order)
    if not targets:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_STATUS[target],
                    callback_data=OwnerCb(order_id=order.id, status=target).pack(),
                )
            ]
            for target in targets
        ]
    )


def owner_card_keyboard(order: Order, page: int = 0) -> InlineKeyboardMarkup | None:
    _, page, pages = page_slice(_owner_lines(order), page, 2)
    nav = [
        InlineKeyboardButton(
            text=label, callback_data=OwnerPageCb(order_id=order.id, page=n).pack()
        )
        for label, n in ((texts.BTN_PREV, page - 1), (texts.BTN_NEXT, page + 1))
        if 0 <= n < pages
    ]
    rows = [nav] if nav else []
    actions = owner_keyboard(order)
    if actions is not None:
        rows.extend(actions.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def notify_owners(bot: Bot, owner_ids: Iterable[int], order: Order) -> None:
    """Отправить владельцам карточку нового заказа. Сбой у одного не мешает остальным."""
    text = render_owner_order(order)
    markup = owner_card_keyboard(order)
    for owner_id in sorted(owner_ids):
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("owner %s not notified about order %s: %s", owner_id, order.id, exc)


async def _refresh(callback: CallbackQuery, order: Order, page: int = 0) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                render_owner_order(order, page), reply_markup=owner_card_keyboard(order, page)
            )
        except TelegramBadRequest:
            pass  # "message is not modified"


@router.message(Command("owner"))
async def cmd_owner(
    message: Message, orders: OrdersRepo, owner_ids: frozenset[int] = frozenset()
) -> None:
    if message.from_user is None or message.from_user.id not in owner_ids:
        await message.answer(texts.NO_ACCESS)
        return
    counts = await orders.count_by_status()
    lines = [
        texts.OWNER_SUMMARY.format(
            pending=counts.get("new", 0) + counts.get("pending", 0),
            active=counts.get("paid", 0) + counts.get("shipped", 0),
        )
    ]
    unsynced = await orders.count_unsynced()
    if unsynced:
        lines.append(texts.OWNER_SUMMARY_UNSYNCED.format(unsynced=unsynced))
    await message.answer("\n".join(lines))


@router.callback_query(OwnerPageCb.filter())
async def on_owner_page(
    callback: CallbackQuery,
    callback_data: OwnerPageCb,
    orders: OrdersRepo,
    owner_ids: frozenset[int] = frozenset(),
) -> None:
    if callback.from_user.id not in owner_ids:
        await callback.answer(texts.NO_ACCESS, show_alert=True)
        return
    order = await orders.get_by_id(callback_data.order_id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _refresh(callback, order, callback_data.page)


@router.callback_query(OwnerCb.filter())
async def on_status(
    callback: CallbackQuery,
    callback_data: OwnerCb,
    orders: OrdersRepo,
    panel_client: PanelClient | None = None,
    owner_ids: frozenset[int] = frozenset(),
) -> None:
    if callback.from_user.id not in owner_ids:
        await callback.answer(texts.NO_ACCESS, show_alert=True)
        return
    order = await orders.get_by_id(callback_data.order_id)
    if order is None:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.panel_order_id is None or panel_client is None:
        await callback.answer(texts.OWNER_NO_PANEL, show_alert=True)
        return

    target = callback_data.status
    if target not in next_statuses(order):
        # Кнопка от старого сообщения: статус уже ушёл вперёд.
        await callback.answer(texts.OWNER_STALE, show_alert=True)
        await _refresh(callback, order)
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
            new_status = await panel_client.patch_status(order.panel_order_id, target)
    except PanelConflictError as exc:
        if exc.current_status in TRANSITIONS:
            await orders.set_status(order.id, exc.current_status)
            await _refresh(callback, replace(order, status=exc.current_status))
        if isinstance(callback.message, Message):
            await callback.message.answer(texts.OWNER_CONFLICT)
        return
    except PanelError as exc:
        log.warning("status change failed for order %s: %s", order.id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(texts.OWNER_PANEL_DOWN)
        return

    if new_status not in TRANSITIONS:
        new_status = target
    await orders.set_status(order.id, new_status)
    await _refresh(callback, replace(order, status=new_status))
