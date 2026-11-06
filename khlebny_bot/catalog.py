"""Общее меню с orders-admin-panel; цены в копейках.

ID 7 в панели зарезервирован для снятой с продажи подписки.
При включённой интеграции main загружает каталог панели и периодически обновляет его.
"""

CATALOG: list[dict] = [
    {"id": 1, "name": "Медовик, 1 кг", "price_cents": 240000},
    {"id": 2, "name": "Чизкейк «Нью-Йорк»", "price_cents": 320000},
    {"id": 3, "name": "Набор круассанов (6 шт.)", "price_cents": 115000},
    {"id": 4, "name": "Эклеры (8 шт.)", "price_cents": 144000},
    {"id": 5, "name": "Капкейки (12 шт.)", "price_cents": 216000},
    {"id": 6, "name": "Вишнёвый пирог", "price_cents": 110000},
    {"id": 8, "name": "Набор имбирного печенья", "price_cents": 78000},
    {"id": 9, "name": "Фокачча с розмарином", "price_cents": 39000},
    {"id": 10, "name": "Ржаной хлеб на закваске", "price_cents": 14000},
    {"id": 11, "name": "Пшеничный батон", "price_cents": 9000},
    {"id": 12, "name": "Багет", "price_cents": 11000},
    {"id": 13, "name": "Чиабатта", "price_cents": 13000},
    {"id": 14, "name": "Бородинский хлеб", "price_cents": 12500},
    {"id": 15, "name": "Круассан с маслом", "price_cents": 20000},
    {"id": 16, "name": "Булочка с корицей", "price_cents": 14000},
    {"id": 17, "name": "Пирожок с яблоком", "price_cents": 8000},
    {"id": 18, "name": "Ватрушка с творогом", "price_cents": 10500},
    {"id": 19, "name": "Синнабон со сливочным кремом", "price_cents": 24000},
    {"id": 20, "name": "Слойка с сыром", "price_cents": 16000},
    {"id": 21, "name": "Печенье с шоколадом (3 шт.)", "price_cents": 21000},
]


def get_item(item_id: int | str) -> dict | None:
    for item in CATALOG:
        if str(item["id"]) == str(item_id):
            return item
    return None
