# khlebny-den-bot

[![CI](https://github.com/kuv8lda/khlebny-den-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/kuv8lda/khlebny-den-bot/actions/workflows/ci.yml)

[Русский](README.ru.md) · [Deployment guide (RU)](docs/DEPLOY.md) · [MIT](LICENSE)

Telegram demo bot for the fictional bakery «Хлебный день», built with aiogram 3 and long polling. Orders have no real fulfilment or payments.

Customers browse the menu, edit their cart and confirm an order. The bot stores orders and conversation state in SQLite. Connect orders-admin-panel to send orders, receive status updates and let bakery owners change order statuses from Telegram.

## Run

You need Python 3.11+ and a bot token from @BotFather.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Set `BOT_TOKEN` in `.env`, then run:

```bash
python -m khlebny_bot.main
```

For Docker or Compose, follow the [deployment guide](docs/DEPLOY.md). Compose requires a separate, prebuilt panel image.

## Configuration

Set these variables in the environment or `.env` (gitignored):

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | Required | Telegram bot token |
| `FSM_DB_PATH` | `data/fsm.sqlite3` | Conversation state and carts |
| `ORDERS_DB_PATH` | Same as `FSM_DB_PATH` | Orders, event cursor and price notification state |
| `PANEL_BASE_URL` | `http://127.0.0.1:5000` | Panel address reachable from the bot |
| `PANEL_API_KEY` | Empty | Bearer token; leave empty to disable integration |
| `PANEL_TIMEOUT_SECONDS` | `5` | Timeout per panel request attempt |
| `EVENTS_POLL_INTERVAL` | `5` | Event and competitor-price polling interval, seconds; must be positive and finite |
| `CATALOG_TTL_SECONDS` | `60` | Catalogue cache TTL, seconds |
| `OWNER_TELEGRAM_IDS` | Empty | Comma-separated owner IDs, e.g. `123,456`; invalid values stop startup |

The bot logs at `INFO`; startup does not use the `LOG_LEVEL` setting.

## Commands

| Command | Action |
|---|---|
| `/start` | Show the menu, cart and order-history buttons |
| `/menu` | Browse products, five per page; tap to add one |
| `/cart` | Change quantities (1–20 per product), remove items or start checkout |
| `/orders` | Show the last five orders and their details |
| `/cancel` | Cancel unfinished checkout and keep the cart |
| `/help` | Show command help |
| `/privacy` | Read the data-storage policy |
| `/forget` | Delete local customer data and request panel anonymisation |
| `/owner` | Show the owner's order summary |
| `/competitor` | Show competitor prices from the panel |

Checkout asks for a name (1–40 characters), an optional comment (up to 200) and confirmation. The bot saves a snapshot of the products and prices shown at confirmation. It does not request a phone number.

Owner commands require an ID in `OWNER_TELEGRAM_IDS` and do not appear in the public Telegram command menu. Owners must message the bot before it can send them notifications.

## Catalogue

The offline catalogue contains 20 demo products shared with the panel. Product ID 7 is retired.

With integration enabled, the bot loads the panel catalogue before polling. If the request fails, startup fails to avoid sending orders with stale product IDs. Restart the bot after changing the panel's prices or assortment. Catalogue changes clear unfinished carts and checkouts; saved orders keep their snapshots. Open `/menu` to replace old buttons.

## Panel integration

The bot uses Bearer authentication for `/api/integrations/*`:

- `GET /catalog`: product array with `id`, `name` and `price_cents`.
- `POST /orders`: order creation with a saved checkout token as `Idempotency-Key`.
- `GET /events?since_id=N&limit=50`: status events in ascending ID order.
- `PATCH /orders/<id>/status`: owner action with `{"status": "..."}`.

### Order delivery and retries

The bot saves the order before sending it to the panel. Network errors and HTTP 5xx trigger up to three attempts, with delays of 0.25 and 0.5 seconds. Each attempt uses `PANEL_TIMEOUT_SECONDS`. The client treats HTTP 200/409 on order creation as a duplicate; it does not retry other HTTP 4xx responses.

If delivery fails, the order keeps `pending_panel_sync=1`. The bot clears the cart and checkout, then offers «Повторить отправку» in the result message and order details. A retry sends the saved order with the same key. There is no background order-delivery queue.

### Status notifications

One event worker per order database polls the panel and sends customers status updates. It saves the event cursor and local order status in one transaction after sending the message. A crash between Telegram delivery and the SQLite commit can cause a duplicate notification.

On a panel or Telegram error, the worker retries in the next cycle without advancing past the failed event. It skips blocked recipients, unknown orders and unknown statuses, logging the reason. Local updates appear in `/orders` and `/owner`.

Owners receive new-order cards with available status actions:

```text
pending → paid or cancelled
paid → shipped or cancelled
shipped → completed
```

The local `new` status maps to `pending`. Orders without a panel ID have no status buttons. On HTTP 409, the bot reports the conflict and refreshes the card if the panel supplied its current status. Owner notification failures do not roll back a customer's order.

### Competitor prices

This feature requires `GET /api/integrations/competitor-prices` in the panel. Add it to panels that do not support it. The panel or its parser must match competitor products to local products.

The endpoint returns an array:

```json
[
  {
    "competitor_id": 1,
    "competitor_name": "Neighbour bakery",
    "product_id": 2,
    "product_name": "Croissant",
    "price_cents": 22000,
    "own_price_cents": 20000
  }
]
```

IDs must be positive integers, names must contain 1–200 characters and prices must be non-negative integers. `own_price_cents` is optional and may be null.

A separate worker compares consecutive processed prices per owner and notifies them of changes **greater than 5%**. The first snapshot sets the baseline. A change of 5% or less updates the baseline without a notification. A zero baseline also updates without a percentage notification.

SQLite stores each owner's baseline. Temporary Telegram failures leave that owner's notification for the next poll; blocked recipients are skipped. A crash after delivery but before saving can cause a duplicate. Polling cannot capture price changes between snapshots. Run one worker per database; there are no daily digests.

`/competitor` reads the panel's current snapshot without changing notification state and reports missing data or panel errors.

## Privacy

`/forget` deletes the customer's local orders, items, carts and FSM records across chats before requesting panel anonymisation. On a panel error, repeat the command later; there is no retry queue.

The panel clears the name, phone and username but retains Telegram ID, comments, orders, totals and events. The command does not delete Telegram messages or backups.

Cleanup runs at startup and hourly. It removes carts and unfinished checkout data after seven days without changes. Migrated FSM records start a new retention period. Order history remains until `/forget`.

Order-rate counters contain Telegram ID and timestamps. They survive `/forget` to prevent quota bypass and expire within two hours while the bot runs.

## Limits and shutdown

- Checkout allows five new orders per rolling hour. Retrying an existing order does not consume the quota. SQLite preserves counters across restarts.
- Incoming messages and callbacks share a per-user limit of one event per 0.5 seconds. The bot ignores excess messages and answers excess callbacks with an explanation.
- FSM isolation serialises actions for the same user in the same chat.
- On SIGTERM/SIGINT, the bot stops polling, waits for handlers and workers, then closes sessions and databases.

## Development

```bash
ruff check .
pytest -q
```

Tests use mocks and do not require network access. CI runs Ruff and pytest on Python 3.11, 3.12 and 3.13, then builds a Docker image and checks non-root volume writes. Dependencies use version ranges in `pyproject.toml`; there is no dependency lockfile.

See [design decisions](DECISIONS.md), [Telegram UX rules](docs/TELEGRAM_UX.md) and the [live acceptance checklist](docs/DEPLOY.md). Mock tests do not measure Telegram latency or verify a running panel.
