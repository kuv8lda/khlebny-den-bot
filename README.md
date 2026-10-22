# khlebny-den-bot

[![CI](https://github.com/kuv8lda/khlebny-den-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/kuv8lda/khlebny-den-bot/actions/workflows/ci.yml)

[Русский](README.ru.md) · [Deployment](docs/DEPLOY.md) · [MIT](LICENSE)

Demo Telegram bakery bot built with aiogram 3, Python 3.11+ and SQLite (long polling).
The bakery is fictional: no real payments or fulfilment. Optional integration with
`orders-admin-panel` provides order creation, owner status buttons and customer notifications.
Stages 1–4 and D–H are implemented; live Telegram/panel acceptance remains a separate check.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# Set BOT_TOKEN from @BotFather in .env, then:
python -m khlebny_bot.main
```

Leave `PANEL_API_KEY` empty for standalone mode. Never commit `.env`.
For Docker or the two-service Compose deployment, follow [DEPLOY.md](docs/DEPLOY.md)
first: Compose requires a separately built panel image and its configuration.

## Features and commands

- `/start`, `/help`: introduction and navigation.
- `/menu`: nine demo products; `/cart`: quantities (1–20), totals and checkout.
- Checkout: name (1–40 characters), optional comment (up to 200), confirmation.
- “My orders” shows the latest five orders and their details.
- `/cancel`: reset checkout without losing the cart.
- `/owner`: owner-only local summary. New-order cards include status buttons when
  the panel ID is known. Owners must first message the bot. Phone numbers are not collected.
- `/privacy`: storage and disclosure notice; `/forget`: local deletion and a panel
  anonymization request.

Owner transitions: `pending → paid|cancelled`, `paid → shipped|cancelled`,
`shipped → completed`. Unauthorized users cannot change statuses. HTTP 409 is
explained to the owner without changing local state.

## Configuration

Environment variables override `.env` (see [.env.example](.env.example)).

| Variable | Default / meaning |
| --- | --- |
| `BOT_TOKEN` | Required Telegram token |
| `FSM_DB_PATH` | `data/fsm.sqlite3` |
| `ORDERS_DB_PATH` | Empty: use the FSM database |
| `PANEL_BASE_URL` | `http://127.0.0.1:5000` |
| `PANEL_API_KEY` | Empty: disable panel integration |
| `PANEL_TIMEOUT_SECONDS` | `5`, timeout per HTTP attempt |
| `EVENTS_POLL_INTERVAL` | `5`, positive finite seconds |
| `CATALOG_TTL_SECONDS` | `60`, catalog cache TTL |
| `OWNER_TELEGRAM_IDS` | Empty; otherwise comma-separated Telegram IDs |

Logging is currently fixed at INFO; the `LOG_LEVEL` settings field is not wired to startup.
Compose overrides database paths to `/app/data/` and the panel URL to its service hostname.
Compose-only settings are documented in the deployment guide.

## Panel integration and reliability

Requests use Bearer authentication. Endpoints:

- `GET /api/integrations/catalog`: catalog with TTL cache and stale/static fallback.
- `POST /api/integrations/orders`: creation with the persisted checkout token as
  `Idempotency-Key`; 200/409 are treated as duplicates.
- `PATCH /api/integrations/orders/<id>/status`: owner transitions.
- `GET /api/integrations/events?since_id=N&limit=50`: status polling, SQLite cursor.
- `POST /api/integrations/forget`: anonymization request.

Network errors and HTTP 5xx receive up to three attempts with 0.25/0.5-second
backoff; HTTP 4xx are not retried. On failure the local order is marked
`pending_panel_sync`; the cart and confirmation remain intact. The customer must
press Confirm again to resend the same order with the same key. There is no background outbox.

Checkout allows five new orders per user in a rolling hour. Persistent counters
survive restarts and `/forget`; duplicate confirmation does not consume the quota.
Messages/buttons are throttled to one event per user per 0.5 seconds.
SIGTERM/SIGINT stop polling, drain handlers and stop workers before closing resources.

Events notify customers about pending, paid, shipped, completed and cancelled orders.
Already processed IDs are skipped. Delivery failures are retried on a later poll;
blocked recipients are skipped. Unknown orders/statuses are logged and skipped.
The cursor is saved **after** sending: a crash between Telegram delivery and SQLite
commit can cause a duplicate. This is not an exactly-once guarantee.
Run only one bot/event worker per token and database.

## Privacy

Local storage includes Telegram IDs, names, comments, order items/totals, carts,
FSM data and the event cursor. `/forget` deletes local orders and FSM/cart data
across chats and requests panel anonymization. On panel failure, local deletion
has already happened; repeat the command later. There is no automatic retry queue.

The panel anonymizes name, phone and username, but retains Telegram ID, comments,
orders, totals and events. Telegram messages and backups are not deleted.
Inactive carts/FSM are removed after seven days, checked at startup and hourly.
Orders have no automatic retention limit. Anti-abuse counters retain ID/time after
`/forget` and are cleaned at startup/hourly when older than an hour (up to two hours
while running). The privacy command discloses this exception.

## Development

```bash
ruff check .
pytest -q
```

Tests mock Telegram and HTTP; they do not require live credentials. CI tests Python
3.11–3.13 and builds the Docker image, checking its non-root user and writable data
volume. Dependency ranges live in `pyproject.toml`; there is no lock file.

Modules: `handlers` (menu/cart), `checkout` (FSM), `orders`/`storage` (SQLite),
`panel` (HTTP/cache), `owner`, `events`, `privacy`, `reliability`, `config`, `texts`.
See [DECISIONS.md](DECISIONS.md) for design choices and the
[Russian guide](README.ru.md) for detailed behavior. Licensed under [MIT](LICENSE).
