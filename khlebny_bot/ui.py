"""Пагинация и форматирование текста для Telegram."""

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def page_slice(items: Sequence[T], page: int, size: int) -> tuple[Sequence[T], int, int]:
    """Ограничить страницу: старые кнопки не должны открывать пустой экран."""
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size : (page + 1) * size], page, pages


def one_line(text: str) -> str:
    """Заменить последовательности пробельных символов одним пробелом."""
    return " ".join(text.split())
