"""Клиент панели orders-admin-panel.

GET  /api/integrations/catalog  — загрузка каталога.
POST /api/integrations/orders   — отправка нового заказа в панель.
PATCH /api/integrations/orders/<id>/status — смена статуса заказа владельцем.

Ответ каталога — голый массив [{"id": int, "name": str, "price_cents": int}].
Признака «недоступна» у позиции нет.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field, StrictInt, TypeAdapter, ValidationError

from .config import Settings

log = logging.getLogger(__name__)

EVENTS_PATH = "/api/integrations/events"
CATALOG_PATH = "/api/integrations/catalog"
ORDERS_PATH = "/api/integrations/orders"
ORDER_STATUS_PATH = ORDERS_PATH + "/{order_id}/status"


class PanelError(Exception):
    """Панель недоступна или вернула ответ не в ожидаемом формате."""


class PanelConflictError(PanelError):
    """Панель отказала в смене статуса (409): переход недопустим или заказ уже изменён."""

    def __init__(self, message: str, current_status: str | None = None) -> None:
        super().__init__(message)
        self.current_status = current_status  # если панель сообщила текущий статус


class _CatalogItem(BaseModel):
    id: StrictInt = Field(ge=1)
    name: str = Field(min_length=1)
    price_cents: StrictInt = Field(ge=0)


_CATALOG_ADAPTER = TypeAdapter(list[_CatalogItem])


def parse_catalog(payload: object) -> list[dict]:
    """Разобрать голый массив. id остаётся int: строки вроде "1" не принимаются."""
    try:
        items = _CATALOG_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise PanelError(f"unexpected catalog format: {exc.error_count()} error(s)") from exc
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise PanelError("duplicate item ids in catalog")
    return [item.model_dump() for item in items]


# ---------------------------------------------------------------------------
# Модели заказа
# ---------------------------------------------------------------------------


class OrderItemPayload(BaseModel):
    product_id: int
    quantity: int


class OrderPayload(BaseModel):
    customer_name: str
    items: list[OrderItemPayload]
    telegram_user_id: int


class PanelOrderResult(BaseModel):
    """Ответ панели на создание/поиск заказа."""

    panel_order_id: Any = None  # id, возвращённый панелью (int или str)
    duplicate: bool = False     # True если панель вернула 200 (дубль) или 409


# ---------------------------------------------------------------------------
# PanelClient
# ---------------------------------------------------------------------------


class PanelEvent(BaseModel):
    id: StrictInt = Field(ge=1)
    order_id: StrictInt = Field(ge=1)
    status: str = Field(min_length=1)
    created_at: str


_EVENTS_ADAPTER = TypeAdapter(list[PanelEvent])


class PanelClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 5,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def fetch_events(self, since_id: int, limit: int = 50) -> list[PanelEvent]:
        """События Telegram-заказов после курсора, по возрастанию id."""
        try:
            response = await self._client.get(
                EVENTS_PATH, params={"since_id": since_id, "limit": limit}
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise PanelError(f"panel returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise PanelError(f"panel events request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise PanelError("panel returned invalid JSON") from exc
        try:
            events = _EVENTS_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise PanelError("unexpected events format") from exc
        ids = [event.id for event in events]
        if ids != sorted(set(ids)):
            raise PanelError("event ids must be unique and ordered")
        return events

    async def fetch_catalog(self) -> list[dict]:
        try:
            response = await self._client.get(CATALOG_PATH)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise PanelError(f"panel returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise PanelError(f"panel request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise PanelError("panel returned invalid JSON") from exc
        return parse_catalog(payload)

    async def create_order(self, payload: OrderPayload) -> PanelOrderResult:
        """Отправить заказ в панель.

        Статусы:
          201 — создан,  200 / 409 — дубль (идемпотентность),
          422 — ошибка валидации панели.
        Любой другой HTTP-статус или сетевой сбой — PanelError.
        """
        idempotency_key = str(uuid.uuid4())
        try:
            response = await self._client.post(
                ORDERS_PATH,
                json=payload.model_dump(),
                headers={"Idempotency-Key": idempotency_key},
            )
        except httpx.HTTPError as exc:
            raise PanelError(f"panel order request failed: {type(exc).__name__}") from exc

        if response.status_code == 201:
            try:
                panel_id = response.json().get("id")
            except ValueError:
                panel_id = None
            return PanelOrderResult(panel_order_id=panel_id, duplicate=False)

        if response.status_code in (200, 409):
            try:
                panel_id = response.json().get("id")
            except ValueError:
                panel_id = None
            return PanelOrderResult(panel_order_id=panel_id, duplicate=True)

        if response.status_code == 422:
            raise PanelError(f"panel rejected order (422): {response.text[:200]}")

        raise PanelError(f"panel returned HTTP {response.status_code}")

    async def patch_status(self, order_id: int, status: str) -> str:
        """Сменить статус заказа в панели. Вернуть статус после смены.

        200 — готово (статус берём из ответа, иначе тот, что просили).
        409 — переход недопустим: PanelConflictError.
        Остальное (404, 422, 5xx, сеть) — PanelError.
        """
        try:
            response = await self._client.patch(
                ORDER_STATUS_PATH.format(order_id=order_id), json={"status": status}
            )
        except httpx.HTTPError as exc:
            raise PanelError(f"panel status request failed: {type(exc).__name__}") from exc

        body = _json_object(response)
        reported = body.get("status")
        if response.status_code == 200:
            return reported if isinstance(reported, str) and reported else status
        if response.status_code == 409:
            raise PanelConflictError(
                "panel refused status change (409)",
                reported if isinstance(reported, str) and reported else None,
            )
        raise PanelError(f"panel returned HTTP {response.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()


def _json_object(response: httpx.Response) -> dict:
    """Тело ответа как словарь; всё остальное (не JSON, массив) — пустой словарь."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class CatalogProvider:
    """Каталог с TTL-кэшем. Без клиента или при ошибке панели отдаёт запасной каталог."""

    def __init__(
        self,
        client: PanelClient | None,
        fallback: list[dict],
        ttl_seconds: float = 60,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: list[dict] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> list[dict]:
        if self._client is None:
            return self._fallback
        async with self._lock:
            now = self._clock()
            if self._cached is not None and now < self._expires_at:
                return self._cached
            try:
                self._cached = await self._client.fetch_catalog()
            except PanelError as exc:
                log.warning("catalog from panel unavailable, using fallback: %s", exc)
                # Устаревший кэш лучше хардкода. Повторная попытка — не раньше чем через TTL.
                if self._cached is None:
                    self._cached = self._fallback
            self._expires_at = now + self._ttl
            return self._cached

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_catalog_provider(settings: Settings, fallback: list[dict]) -> CatalogProvider:
    client = None
    if settings.panel_enabled:
        client = PanelClient(
            settings.panel_base_url,
            settings.panel_api_key.get_secret_value().strip(),
            settings.panel_timeout_seconds,
        )
    return CatalogProvider(client, fallback, settings.catalog_ttl_seconds)
