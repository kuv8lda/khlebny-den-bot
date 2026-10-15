"""Клиент панели orders-admin-panel: GET /api/integrations/catalog.

Ответ панели — голый массив [{"id": int, "name": str, "price_cents": int}].
Признака «недоступна» у позиции нет.
"""

import asyncio
import logging
import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, Field, StrictInt, TypeAdapter, ValidationError

from .config import Settings

log = logging.getLogger(__name__)

CATALOG_PATH = "/api/integrations/catalog"


class PanelError(Exception):
    """Панель недоступна или вернула ответ не в ожидаемом формате."""


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

    async def aclose(self) -> None:
        await self._client.aclose()


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
