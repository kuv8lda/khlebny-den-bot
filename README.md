# khlebny-den-bot

Демо Telegram-бот пекарни «Хлебный день» на aiogram 3 (long polling).
Пекарня вымышленная, заказы демонстрационные, реальных платежей нет.
Позже бот будет работать с панелью orders-admin-panel через `/api/integrations/*`.

## Запуск

Нужен Python 3.11+ и токен от @BotFather.

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
cp .env.example .env   # вписать BOT_TOKEN, затем: python -m bot.main
```

Системный `pip` на Debian/Ubuntu ставить пакеты не даёт (PEP 668), поэтому нужен venv.

Через Docker: `docker compose up --build`.

## Проверки

```bash
ruff check .
pytest -q
```

## Структура

- `bot/` — код бота (`main.py`, `handlers.py`, `config.py`)
- `tests/` — тесты
- `DECISIONS.md` — отклонения от ТЗ

## Статус

Этап 0: бот отвечает на `/start` текстом-заглушкой.
