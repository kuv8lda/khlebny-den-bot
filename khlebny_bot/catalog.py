"""Каталог. Пока хардкод; на этапе 3 заменится запросом к панели.

Структура совместима с GET /api/integrations/catalog: [{id, name, price_cents}].
"""

CATALOG: list[dict] = [
    {"id": 1, "name": "Ржаной хлеб на закваске", "price_cents": 14000},
    {"id": 2, "name": "Пшеничный батон", "price_cents": 9000},
    {"id": 3, "name": "Багет", "price_cents": 11000},
    {"id": 4, "name": "Чиабатта", "price_cents": 13000},
    {"id": 5, "name": "Бородинский хлеб", "price_cents": 12500},
    {"id": 6, "name": "Круассан с маслом", "price_cents": 15000},
    {"id": 7, "name": "Булочка с корицей", "price_cents": 9500},
    {"id": 8, "name": "Пирожок с яблоком", "price_cents": 8000},
    {"id": 9, "name": "Ватрушка с творогом", "price_cents": 10500},
]


def get_item(item_id: int | str) -> dict | None:
    for item in CATALOG:
        if str(item["id"]) == str(item_id):
            return item
    return None
