"""Все тексты бота."""

CURRENCY = "₽"

BTN_MENU = "Меню"
BTN_CART = "Корзина"
BTN_ORDERS = "Мои заказы"

BTN_DEC = "−"
BTN_INC = "+"
BTN_REMOVE = "✕"
BTN_CLEAR = "Очистить корзину"
BTN_QTY = "{name}: {qty}"
BTN_CHECKOUT = "Оформить заказ"
BTN_SKIP = "Пропустить"
BTN_CONFIRM = "Подтвердить"
BTN_CANCEL = "Отмена"
BTN_ORDER = "Заказ №{id}"
MENU_BUTTON = "{name} — {price}"

START = (
    "Привет! Это «Хлебный день» 🍞\n\n"
    "Это демо: пекарня вымышленная, заказ не исполняется.\n\n"
    "Выберите, что хотите сделать."
)
HELP = (
    "Команды:\n"
    "/menu — меню пекарни\n"
    "/cart — корзина\n"
    "/cancel — сбросить текущее действие\n"
    "/help — эта справка\n\n"
    "Это демо, заказ не исполняется."
)

MENU_TITLE = "Меню. Нажмите на позицию, чтобы добавить её в корзину:"
ADDED = "Добавлено: {name}"
QTY_MAX = "Максимум {max} шт. одной позиции"
QTY_MIN = "Минимум {min} шт. Чтобы убрать позицию, нажмите «✕»"
UNKNOWN_ITEM = "Такой позиции нет в меню"

CART_TITLE = "Ваша корзина:"
CART_LINE = "{name} × {qty} — {sum}"
CART_TOTAL = "Итого: {total}"
CART_EMPTY = "Корзина пуста. Загляните в /menu"

CANCELLED = "Готово, текущее действие сброшено. Корзина осталась как была."

DEMO_NOTE = "Это демо: заказ не исполняется."

CHECKOUT_ASK_NAME = (
    "Как записать заказ? Напишите имя (от 1 до {max} символов, можно вымышленное).\n\n"
    "Отмена: /cancel"
)
CHECKOUT_NAME_INVALID = "Имя должно быть от 1 до {max} символов. Напишите ещё раз или /cancel"
CHECKOUT_ASK_COMMENT = (
    "Напишите комментарий к заказу (до {max} символов) или нажмите «Пропустить».\n\n"
    "Отмена: /cancel"
)
CHECKOUT_COMMENT_INVALID = (
    "Комментарий — текст до {max} символов. Напишите его или нажмите «Пропустить»."
)
CHECKOUT_CONFIRM_TITLE = "Проверьте заказ:"
CHECKOUT_NAME_LINE = "Имя: {name}"
CHECKOUT_COMMENT_LINE = "Комментарий: {comment}"
NO_COMMENT = "—"
CHECKOUT_STALE = "Это действие уже неактуально. Откройте /cart"
ORDER_CREATED = "Заказ №{id} оформлен. Он появится в «Мои заказы».\n\n" + DEMO_NOTE
ORDER_ALREADY = "Заказ №{id} уже оформлен."

ORDERS_EMPTY = "Заказов пока нет. Загляните в /menu"
ORDERS_TITLE = "Ваши последние заказы:"
ORDER_LINE = "№{id} · {date} · {total} · {status}"
ORDER_TITLE = "Заказ №{id} · {status}"
ORDER_DATE_LINE = "Дата: {date}"
ORDER_NOT_FOUND = "Заказ не найден"
STATUS_NAMES = {"new": "новый"}
CMD_MENU = "Меню пекарни"
CMD_CART = "Корзина"
CMD_CANCEL = "Сбросить действие"
CMD_HELP = "Справка"
