import httpx
import pytest
import respx

from khlebny_bot.catalog import CATALOG
from khlebny_bot.config import Settings
from khlebny_bot.panel import (
    CatalogProvider,
    PanelClient,
    PanelError,
    build_catalog_provider,
    parse_catalog,
)

BASE = "http://127.0.0.1:5000"
URL = f"{BASE}/api/integrations/catalog"
PANEL_ITEMS = [
    {"id": 1, "name": "Ржаной хлеб", "price_cents": 14000},
    {"id": 2, "name": "Багет", "price_cents": 11000},
]


@pytest.fixture
async def client():
    c = PanelClient(BASE, "secret-key", timeout=1)
    yield c
    await c.aclose()


@respx.mock
async def test_fetch_sends_bearer_and_parses_bare_array(client):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=PANEL_ITEMS))
    items = await client.fetch_catalog()
    assert items == PANEL_ITEMS
    assert all(type(i["id"]) is int for i in items)
    assert route.calls.last.request.headers["Authorization"] == "Bearer secret-key"


@respx.mock
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_http_error_becomes_panel_error(client, status):
    respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(PanelError, match=str(status)):
        await client.fetch_catalog()


@respx.mock
async def test_timeout_becomes_panel_error(client):
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(PanelError):
        await client.fetch_catalog()


@respx.mock
async def test_invalid_json_becomes_panel_error(client):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(PanelError, match="JSON"):
        await client.fetch_catalog()


@pytest.mark.parametrize(
    "payload",
    [
        {"items": PANEL_ITEMS},  # обёртка вместо голого массива
        [{"id": "1", "name": "Багет", "price_cents": 100}],  # id строкой
        [{"id": 1.0, "name": "Багет", "price_cents": 100}],  # id не целое
        [{"id": 0, "name": "Багет", "price_cents": 100}],
        [{"id": 1, "name": "", "price_cents": 100}],
        [{"id": 1, "name": "Багет", "price_cents": -1}],
        [{"id": 1, "name": "Багет"}],
        [
            {"id": 1, "name": "A", "price_cents": 1},
            {"id": 1, "name": "B", "price_cents": 2},
        ],
    ],
)
def test_parse_rejects_bad_format(payload):
    with pytest.raises(PanelError):
        parse_catalog(payload)


def test_parse_empty_array_is_valid():
    assert parse_catalog([]) == []


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@respx.mock
async def test_provider_caches_within_ttl_and_refetches_after(client):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=PANEL_ITEMS))
    clock = FakeClock()
    provider = CatalogProvider(client, CATALOG, ttl_seconds=60, clock=clock)
    assert await provider.get() == PANEL_ITEMS
    clock.now = 59
    await provider.get()
    assert route.call_count == 1
    clock.now = 61
    await provider.get()
    assert route.call_count == 2


@respx.mock
async def test_provider_falls_back_when_panel_down(client):
    respx.get(URL).mock(return_value=httpx.Response(500))
    provider = CatalogProvider(client, CATALOG, ttl_seconds=60, clock=FakeClock())
    assert await provider.get() == CATALOG


@respx.mock
async def test_provider_keeps_stale_cache_when_panel_goes_down(client):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=PANEL_ITEMS))
    clock = FakeClock()
    provider = CatalogProvider(client, CATALOG, ttl_seconds=60, clock=clock)
    await provider.get()
    route.mock(return_value=httpx.Response(500))
    clock.now = 100
    assert await provider.get() == PANEL_ITEMS
    # после сбоя повторный запрос не раньше чем через TTL
    clock.now = 120
    await provider.get()
    assert route.call_count == 2


async def test_provider_without_client_uses_fallback():
    provider = CatalogProvider(None, CATALOG)
    assert await provider.get() == CATALOG


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_key_disables_integration(key):
    s = Settings(_env_file=None, bot_token="t", panel_api_key=key)
    assert not s.panel_enabled
    assert build_catalog_provider(s, CATALOG)._client is None


def test_settings_defaults_and_trailing_slash():
    s = Settings(_env_file=None, bot_token="t", panel_api_key="k", panel_base_url=BASE + "/")
    assert s.panel_enabled
    assert s.panel_base_url == BASE
    assert s.panel_timeout_seconds == 5
    assert s.catalog_ttl_seconds == 60
    assert Settings(_env_file=None, bot_token="t").panel_base_url == BASE


def test_api_key_not_in_repr():
    s = Settings(_env_file=None, bot_token="tg-secret", panel_api_key="panel-secret")
    assert "panel-secret" not in repr(s)
    assert "tg-secret" not in repr(s)
