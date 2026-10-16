"""Тесты PanelClient.create_order (respx) и интеграции checkout -> панель."""

import httpx
import pytest
import respx

from khlebny_bot.panel import (
    OrderItemPayload,
    OrderPayload,
    PanelClient,
    PanelError,
    PanelOrderResult,
)

BASE = "http://127.0.0.1:5000"
ORDERS_URL = f"{BASE}/api/integrations/orders"

PAYLOAD = OrderPayload(
    customer_name="Аня",
    items=[
        OrderItemPayload(product_id=1, quantity=2),
        OrderItemPayload(product_id=3, quantity=1),
    ],
    telegram_user_id=42,
)


@pytest.fixture
async def client():
    c = PanelClient(BASE, "secret-key", timeout=1)
    yield c
    await c.aclose()


# ---------------------------------------------------------------------------
# create_order — HTTP-статусы
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_order_201_returns_panel_id(client):
    route = respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(201, json={"id": 7, "status": "new"})
    )
    result = await client.create_order(PAYLOAD)
    assert isinstance(result, PanelOrderResult)
    assert result.panel_order_id == 7
    assert result.duplicate is False
    assert route.called


@respx.mock
async def test_create_order_201_no_id_in_body(client):
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(201, json={}))
    result = await client.create_order(PAYLOAD)
    assert result.panel_order_id is None
    assert result.duplicate is False


@respx.mock
async def test_create_order_200_is_duplicate(client):
    respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    result = await client.create_order(PAYLOAD)
    assert result.duplicate is True
    assert result.panel_order_id == 5


@respx.mock
async def test_create_order_409_is_duplicate(client):
    respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(409, json={"id": 5})
    )
    result = await client.create_order(PAYLOAD)
    assert result.duplicate is True


@respx.mock
async def test_create_order_422_raises_panel_error(client):
    respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(422, json={"detail": "bad items"})
    )
    with pytest.raises(PanelError, match="422"):
        await client.create_order(PAYLOAD)


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_create_order_unexpected_status_raises(client, status):
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(PanelError, match=str(status)):
        await client.create_order(PAYLOAD)


@respx.mock
async def test_create_order_network_error_raises(client):
    respx.post(ORDERS_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(PanelError):
        await client.create_order(PAYLOAD)


# ---------------------------------------------------------------------------
# Заголовки запроса
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_order_sends_bearer_and_idempotency_key(client):
    route = respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    await client.create_order(PAYLOAD)
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer secret-key"
    idempotency_key = req.headers.get("Idempotency-Key", "")
    assert len(idempotency_key) == 36  # UUID4 формат


@respx.mock
async def test_create_order_idempotency_key_differs_per_call(client):
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    await client.create_order(PAYLOAD)
    await client.create_order(PAYLOAD)
    calls = respx.calls
    keys = [c.request.headers.get("Idempotency-Key") for c in calls]
    assert keys[0] != keys[1]


# ---------------------------------------------------------------------------
# Тело запроса
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_order_body_structure(client):
    route = respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    await client.create_order(PAYLOAD)
    body = route.calls.last.request.read()
    import json

    data = json.loads(body)
    assert data["customer_name"] == "Аня"
    assert data["telegram_user_id"] == 42
    assert data["items"] == [
        {"product_id": 1, "quantity": 2},
        {"product_id": 3, "quantity": 1},
    ]


# ---------------------------------------------------------------------------
# Интеграция checkout -> панель (без полного FSM-стека)
# ---------------------------------------------------------------------------


@respx.mock
async def test_checkout_calls_panel_on_new_order(tmp_path):
    """on_confirm вызывает panel_client.create_order при создании нового заказа."""
    from unittest.mock import AsyncMock

    from helpers import make_callback, make_state

    from khlebny_bot import checkout as co
    from khlebny_bot.orders import OrdersRepo

    # Заглушка панели, которая вернёт успешный результат
    mock_client = AsyncMock(spec=PanelClient)
    mock_client.create_order.return_value = PanelOrderResult(panel_order_id=99, duplicate=False)

    orders = OrdersRepo(tmp_path / "o.sqlite3")
    state = make_state()
    await state.update_data(cart={"1": 2, "3": 1})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message_text("Аня"), state)
    await co.on_comment(make_message_text("Без глютена"), state)

    cb = make_callback()
    await co.on_confirm(cb, state, orders, panel_client=mock_client)

    mock_client.create_order.assert_awaited_once()
    call_payload: OrderPayload = mock_client.create_order.call_args.args[0]
    assert call_payload.customer_name == "Аня"
    assert call_payload.telegram_user_id == 1
    assert len(call_payload.items) == 2

    await orders.close()


@respx.mock
async def test_checkout_panel_unavailable_sets_flag(tmp_path):
    """Если панель бросает PanelError — заказ создаётся локально с pending_panel_sync=1."""
    from unittest.mock import AsyncMock

    from helpers import make_callback, make_state

    from khlebny_bot import checkout as co
    from khlebny_bot.orders import OrdersRepo

    mock_client = AsyncMock(spec=PanelClient)
    mock_client.create_order.side_effect = PanelError("connection refused")

    orders = OrdersRepo(tmp_path / "o.sqlite3")
    state = make_state()
    await state.update_data(cart={"1": 2, "3": 1})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message_text("Аня"), state)
    await co.on_skip(make_callback(), state)  # комментарий пропускаем

    cb = make_callback()
    await co.on_confirm(cb, state, orders, panel_client=mock_client)

    (order,) = await orders.list_for_user(1, 10)
    full = await orders.get(order.id, 1)
    assert full is not None
    assert full.pending_panel_sync is True

    await orders.close()


async def test_checkout_no_panel_client_order_created(tmp_path):
    """Без panel_client заказ создаётся нормально, панель не вызывается."""
    from helpers import make_callback, make_state

    from khlebny_bot import checkout as co
    from khlebny_bot.orders import OrdersRepo

    orders = OrdersRepo(tmp_path / "o.sqlite3")
    state = make_state()
    await state.update_data(cart={"1": 2, "3": 1})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message_text("Аня"), state)
    await co.on_comment(make_message_text("Без глютена"), state)

    await co.on_confirm(make_callback(), state, orders, panel_client=None)

    (order,) = await orders.list_for_user(1, 10)
    assert order.status == "new"
    assert order.pending_panel_sync is False

    await orders.close()


async def test_checkout_duplicate_confirm_does_not_call_panel(tmp_path):
    """Повторное подтверждение (тот же токен) не вызывает панель."""
    from unittest.mock import AsyncMock

    from helpers import make_callback, make_state

    from khlebny_bot import checkout as co
    from khlebny_bot.orders import OrdersRepo

    mock_client = AsyncMock(spec=PanelClient)
    mock_client.create_order.return_value = PanelOrderResult(panel_order_id=1, duplicate=False)

    orders = OrdersRepo(tmp_path / "o.sqlite3")
    state = make_state()
    await state.update_data(cart={"1": 2})
    await co.on_checkout_start(make_callback(), state)
    await co.on_name(make_message_text("Аня"), state)
    await co.on_skip(make_callback(), state)  # комментарий пропускаем

    # Сохраним состояние для повтора
    saved_data = await state.get_data()
    saved_fsm = await state.get_state()

    await co.on_confirm(make_callback(), state, orders, panel_client=mock_client)
    assert mock_client.create_order.call_count == 1

    # Симулируем повторное нажатие — восстанавливаем состояние
    await state.set_data(saved_data)
    await state.set_state(saved_fsm)
    await co.on_confirm(make_callback(), state, orders, panel_client=mock_client)
    # Второй вызов — заказ дубль (created=False), панель не должна вызываться
    assert mock_client.create_order.call_count == 1

    await orders.close()


# ---------------------------------------------------------------------------
# Вспомогательная функция для тестов checkout
# ---------------------------------------------------------------------------


def make_message_text(text: str):
    """Создать mock-сообщение с заданным текстом."""
    from helpers import make_message

    return make_message(text)
