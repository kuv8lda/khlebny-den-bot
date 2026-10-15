"""Чистая логика корзины. Корзина: {str(item_id): qty}, в JSON-совместимом виде."""

from . import texts
from .catalog import get_item

MIN_QTY = 1
MAX_QTY = 20

Cart = dict[str, int]


def format_rub(cents: int) -> str:
    """115000 -> '1 150 ₽'; 115050 -> '1 150,50 ₽'."""
    rub, kop = divmod(cents, 100)
    text = f"{rub:,}".replace(",", " ")
    if kop:
        text += f",{kop:02d}"
    return f"{text} {texts.CURRENCY}"


def add_item(cart: Cart, item_id: int | str) -> bool:
    key = str(item_id)
    if get_item(key) is None:
        return False
    qty = cart.get(key, 0)
    if qty >= MAX_QTY:
        return False
    cart[key] = qty + 1
    return True


def set_qty(cart: Cart, item_id: int | str, qty: int) -> bool:
    key = str(item_id)
    if key not in cart or not MIN_QTY <= qty <= MAX_QTY:
        return False
    cart[key] = qty
    return True


def remove_item(cart: Cart, item_id: int | str) -> bool:
    return cart.pop(str(item_id), None) is not None


def clear(cart: Cart) -> None:
    cart.clear()


def total_cents(cart: Cart) -> int:
    total = 0
    for key, qty in cart.items():
        item = get_item(key)
        if item is not None:
            total += item["price_cents"] * qty
    return total
