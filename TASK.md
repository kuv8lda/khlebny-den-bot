# Блок 2: Интеграция бота с панелью

Работаешь ТОЛЬКО в khelbny-den-bot/. Панель не трогаешь.
Предусловие: этапы A, B, C в панели закрыты.

---

## Этап D: сценарий владельца

### Контекст
- Бот отправляет заказы в панель (этап 4).
- В панели есть PATCH /api/integrations/orders/<id>/status.
- FSM: pending → paid|cancelled, paid → shipped|cancelled, shipped → completed.
- Владельцы задаются OWNER_TELEGRAM_IDS (список ID).

### Задача
1. При новом заказе бот отправляет сообщение владельцу:
   номер, имя, телефон, состав, сумма, дата, комментарий.
   Инлайн-кнопки допустимых следующих статусов (по FSM).
2. Нажатие кнопки вызывает PATCH /api/integrations/orders/<id>/status.
3. Сообщение владельцу обновляется (новый статус, новые кнопки).
4. При 409 — понятный текст владельцу.
5. Команда /owner: сводка (сколько pending, сколько в работе).
6. Нажатия от не-владельцев — «нет доступа».

### Модули
- khlebny_bot/owner.py — новый модуль с роутером.
- khlebny_bot/panel.py — метод patch_status(order_id, status).
- khlebny_bot/texts.py — новые тексты.
- khlebny_bot/config.py — OWNER_TELEGRAM_IDS.
- main.py — подключить роутер.

### Тесты
- Новый заказ → уведомление владельцу.
- Кнопки по FSM.
- Нажатие → PATCH → сообщение обновлено.
- 409 → понятный текст.
- /owner.
- Не-владелец → «нет доступа».

### Приёмка
- ruff и pytest зелёные.
- Вживую: заказ → владелец получил уведомление → кнопка → статус изменился.

### После этапа
- DECISIONS.md, README.md, README.ru.md.
- Коммит: feat: owner notifications and status buttons.

---

## Этап E: опрос событий и оповещения клиента

### Задача
1. Бот опрашивает GET /api/integrations/events?since_id=N раз в 5 секунд
   (EVENTS_POLL_INTERVAL в конфиге).
2. Хранит последний обработанный id в SQLite.
3. При новом событии отправляет клиенту:
   pending → «Ожидает оплаты», paid → «Оплачен»,
   shipped → «Передан в доставку», completed → «Выполнен»,
   cancelled → «Отменён».
4. Одно событие — одно сообщение.

### Модули
- khlebny_bot/events.py — фоновый воркер.
- khlebny_bot/panel.py — метод fetch_events(since_id, limit).
- khlebny_bot/texts.py — тексты статусов.
- main.py — запуск воркера, корректная остановка.

### Тесты
- Опрос с курсором.
- Новое событие → сообщение клиенту.
- Повторный опрос не дублирует.
- Панель недоступна → воркер не падает.

### После этапа
- Коммит: feat: poll panel events and notify customers.

---

## Этап F: приватность

### Задача
1. /privacy — объясняет, что хранится и зачем.
2. /forget — удаляет данные клиента из бота, вызывает POST /api/integrations/forget.
3. Автоочистка корзин и FSM старше 7 дней.

### Модули
- khlebny_bot/privacy.py — роутер и фоновая задача.
- khlebny_bot/panel.py — метод forget(telegram_user_id).
- main.py — подключить.

### После этапа
- Коммит: feat: privacy commands and data cleanup.

---

## Этап G: надёжность

### Задача
1. Идемпотентность: Idempotency-Key при создании заказа (проверить, что работает).
2. Ретраи: таймаут, экспоненциальный ретрай на 5xx/сетевые.
   При недоступности панели — сообщение клиенту, корзина не теряется.
3. Антиспам: не более 5 заказов в час на пользователя. Троттлинг сообщений.
4. Graceful shutdown по SIGTERM.

### После этапа
- Коммит: feat: retries, rate limit, graceful shutdown.

---

## Этап H: Docker, CI, README, деплой-подготовка

### Задача
1. Dockerfile (python slim, непривилегированный, том data/).
2. docker-compose.yml (бот + панель).
3. .github/workflows/ci.yml (Python 3.11–3.13, ruff, pytest).
4. README.md и README.ru.md — финальные.
5. docs/DEPLOY.md.
6. LICENSE (MIT).
7. CI-бейдж.

### После этапа
- Коммит: chore: docker, ci, docs, license.

---

## После завершения ВСЕХ этапов

1. ruff и pytest зелёные.
2. DECISIONS.md, README обновлены.
3. Пять коммитов.
4. НЕ пушить.
5. Финальный отчёт.
6. Остановиться.
