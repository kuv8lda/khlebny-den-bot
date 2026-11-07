# Bread day: Telegram Bot

A working demo bakery bot built with Python and aiogram 3. The bot takes orders through Telegram and connects to the separate orders-admin-panel: it sends orders and receives status events and the product catalog. The bot uses long polling and stores data in SQLite. Payments and delivery are not included in the demo.

# Bot + panel demonstration
https://github.com/user-attachments/assets/37505b09-64de-4109-abb0-cb2a4fc5f432

## Getting started

You need Python 3.11+, a token from [@BotFather](https://t.me/BotFather), and access to the Telegram API.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Set `BOT_TOKEN` in `.env`. To connect to the panel, set its address and API key, then start the bot:

```bash
python -m khlebny_bot.main
```

## Commands

| Command | Action |
|---|---|
| `/start` | Open the main menu |
| `/menu` | Browse products and add them to the cart |
| `/cart` | Edit the cart or proceed to checkout |
| `/orders` | View the last five orders |
| `/help` | Get help with commands |
| `/cancel` | Cancel checkout and keep the cart |
| `/privacy` | Read the data retention policy |
| `/forget` | Delete local data and request anonymization in the panel |
| `/admin` | Get a link to the admin panel |
| `/owner` | View the owner's order summary |
| `/competitor` | View competitor prices from the panel |

The `/owner` and `/competitor` commands are available to owners listed in `OWNER_TELEGRAM_IDS`. The `/admin` command is available to all users and links to `https://panel.kuv8lda.dev`, which is hard-coded. Access to the panel itself is controlled by the panel. The link address is independent of `PANEL_BASE_URL`.

## Usage

### Customer

1. Send `/start`, open `/menu`, and add products to the cart.
2. In `/cart`, check the quantities and start checkout. Enter your name and optionally a comment, then confirm the order.
3. The bot saves the order and sends it to the panel if the integration is enabled. If sending fails, retry using the button in the message or in the order details under `/orders`.
4. When the status changes in the panel, the bot sends a Telegram notification.

### Owner

1. Add your Telegram ID to `OWNER_TELEGRAM_IDS` and send `/start` to the bot to receive messages.
2. The bot sends an order card when a new order arrives. For an order linked to the panel, use the card buttons to change its status: `pending` → `paid` → `shipped` → `completed`. An order with the status `pending` or `paid` can be cancelled.
3. Send `/owner` for a summary or `/admin` to open the panel link. If there is a status conflict, check the order in the panel.

## Environment settings

Set variables in the environment or in the `.env` file. Do not commit `.env` to the repository or share keys with third parties.

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Required Telegram bot token from @BotFather. |
| `PANEL_BASE_URL` | Panel address accessible to the bot. Defaults to `http://127.0.0.1:5000`. Use HTTPS for a remote panel. |
| `PANEL_API_KEY` | Bearer token for the panel API. An empty value disables the integration; the bot uses the built-in catalog. |
| `OWNER_TELEGRAM_IDS` | Comma-separated Telegram IDs of owners, for example `123,456`. An empty value leaves the owner list empty. |

Other settings are listed in `.env.example`. To update the catalog via push, set `CATALOG_PUSH_TOKEN` on both the bot and the panel, and set the bot's receiver address in the panel settings. Without push, the bot fetches the catalog at startup. If the panel is unavailable, the bot uses the saved catalog, or the built-in catalog if no saved catalog exists.

## Running on a server

Install the project in a virtual environment and configure `.env` as described in Getting started. Create a `khlebny` user, give it read access to the project and `.env` and write access to the `data` directory. Set permissions on `.env` to `600`.

Create `/etc/systemd/system/khlebny-bot.service`, replacing the paths with your own:

```ini
[Unit]
Description=Khlebny Den Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=khlebny
WorkingDirectory=/opt/khlebny-den-bot
ExecStart=/opt/khlebny-den-bot/.venv/bin/python -m khlebny_bot.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now khlebny-bot
sudo systemctl status khlebny-bot
journalctl -u khlebny-bot -f
```

Run one bot instance per token and SQLite database. The panel requires a separate installation. For catalog push, ensure the panel can reach the bot's receiver over a secure network.

## Data and limitations

- The bot saves an order before sending it to the panel. If sending fails, the customer can retry; there is no background retry queue.
- `/forget` deletes the customer's local data and requests anonymization in the panel. The panel retains the Telegram ID, comments, and order history. If the panel returns an error, retry the command.
- A customer can create up to five orders per rolling hour. Resending a saved order does not count toward the limit.
- The bot deletes abandoned carts and unfinished checkout data after seven days without changes. Orders are retained until `/forget` is issued.

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest -q
```

Tests use mocks and do not require network access. Additional decisions are documented in [DECISIONS.md](DECISIONS.md), and Telegram interface guidelines are in [docs/TELEGRAM_UX.md](docs/TELEGRAM_UX.md).
```

Тесты используют моки и не требуют доступа к сети. Дополнительные решения описаны в [DECISIONS.md](DECISIONS.md), правила интерфейса Telegram: в [docs/TELEGRAM_UX.md](docs/TELEGRAM_UX.md).
