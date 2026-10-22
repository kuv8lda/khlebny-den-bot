"""Сценарий владельца: уведомление о новом заказе, кнопки статусов, команда /owner.

Статусы и допустимые переходы повторяют FSM панели; окончательное решение за панелью:
если она ответит 409, владелец увидит понятный текст.
Права: только Telegram ID из OWNER_TELEGRAM_IDS (в хендлеры приходит как `owner_ids`).
"""

import logging
from collections.abc import Iterable
from dataclasses import replace

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import cart as cart_logic
from . import texts
from .checkout import _item_lines, format_dt
from .orders import Order, OrdersRepo
from .panel import PanelClient, PanelConflictError, PanelError

log = logging.getLogger(__name__)

router = Router()

# pending → paid|cancelled, paid → shipped|cancelled, shipped → completed.
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
    """Статусы, на которые можно перейти. Без связи с панелью менять нечего."""
    if order.panel_order_id is None:
        return ()
    return TRANSITIONS.get(effective_status(order.status), ())


def render_owner_order(order: Order) -> str:
    status = texts.STATUS_NAMES.get(effective_status(order.status), order.status)
    lines = [
        texts.ORDER_TITLE.format(id=order.id, status=status),
        texts.ORDER_DATE_LINE.format(date=format_dt(order.created_at)),
        texts.CHECKOUT_NAME_LINE.format(name=order.name),
        texts.CHECKOUT_COMMENT_LINE.format(comment=order.comment or texts.NO_COMMENT),
        "",
        *_item_lines(order.items),
        "",
        texts.CART_TOTAL.format(total=cart_logic.format_rub(order.total_cents)),
    ]
    if order.panel_order_id is None:
        lines += ["", texts.OWNER_NOT_SYNCED]
    return "\n".join(lines)


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


async def notify_owners(bot: Bot, owner_ids: Iterable[int], order: Order) -> None:
    """Отправить владельцам карточку нового заказа. Сбой у одного не мешает остальным."""
    text = render_owner_order(order)
    markup = owner_keyboard(order)
    for owner_id in sorted(owner_ids):
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("owner %s not notified about order %s: %s", owner_id, order.id, exc)


async def _refresh(callback: CallbackQuery, order: Order) -> None:
    """Перерисовать карточку заказа: новый статус и новые кнопки."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(
                render_owner_order(order), reply_markup=owner_keyboard(order)
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
        await _refresh(callback, order)
        await callback.answer(texts.OWNER_STALE, show_alert=True)
        return

    try:
        new_status = await panel_client.patch_status(order.panel_order_id, target)
    except PanelConflictError as exc:
        if exc.current_status in TRANSITIONS:
            await orders.set_status(order.id, exc.current_status)
            await _refresh(callback, replace(order, status=exc.current_status))
        await callback.answer(texts.OWNER_CONFLICT, show_alert=True)
        return
    except PanelError as exc:
        log.warning("status change failed for order %s: %s", order.id, exc)
        await callback.answer(texts.OWNER_PANEL_DOWN, show_alert=True)
        return

    if new_status not in TRANSITIONS:
        new_status = target
    await orders.set_status(order.id, new_status)
    await _refresh(callback, replace(order, status=new_status))
    await callback.answer(texts.OWNER_STATUS_CHANGED.format(status=texts.STATUS_NAMES[new_status]))
