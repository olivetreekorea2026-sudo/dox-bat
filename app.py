"""DOX Global: запуск существующего bot.py с выбором оплаты.

Загрузите этот файл в GitHub вместо старого app.py.
Реквизиты и BOT_TOKEN задаются ТОЛЬКО в приватном .env на сервере.
Основной bot.py и функции CREATOR01/CREATOR02 остаются без изменений.
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
load_dotenv(BASE_DIR / ".env")

if not os.getenv("BOT_TOKEN"):
    raise SystemExit("BOT_TOKEN не найден: добавьте его в приватный .env на сервере.")

os.environ.setdefault("DB_PATH", str(BASE_DIR / "bot.db"))

# Импорт регистрирует все существующие обработчики заказов и креаторов,
# но НЕ запускает polling: в bot.py он под защитой if __name__ == '__main__'.
import bot as dox  # noqa: E402
from aiogram import F  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402


# --- DOX SETS: аксессуары не продаются отдельно ---
# В оригинальном bot.py после выбора линз есть устаревший пункт
# "Аксессуары" (раствор, дорожный контейнер, палочка для линз).
# Не показываем его клиентам: подарки будут входить в выбранный сет,
# но не должны добавляться как самостоятельные позиции заказа.
# При этом таблицы в базе и старые заказы не удаляем.
_removed_accessory_handlers = (
    dox.item_done,
    dox.acc_pick,
    dox.acc_qty_button,
    dox.acc_done,
    dox.order_back_from_accessories,
    dox.order_back_from_acc_qty,
)
for _handler in tuple(dox.dp.callback_query.handlers):
    if _handler.callback in _removed_accessory_handlers:
        dox.dp.callback_query.handlers.remove(_handler)

# Запрещаем и ввод количества аксессуаров через сообщение.
for _handler in tuple(dox.dp.message.handlers):
    if _handler.callback is dox.acc_qty_text:
        dox.dp.message.handlers.remove(_handler)


async def item_done_no_accessories(callback: CallbackQuery, state: FSMContext):
    """Сразу показываем проверку линз, без отдельной продажи аксессуаров."""
    await state.update_data(accessories={}, acc_current=None)
    await callback.message.edit_reply_markup()
    await dox._show_summary(callback.message, state)
    await callback.answer()


dox.dp.callback_query.register(
    item_done_no_accessories,
    F.data == "item_done",
    dox.OrderForm.more,
)


PAYMENT_METHODS = {
    "e9": (
        "🏦 Сбербанк / E9pay",
        (
            "🏦 Сбербанк / E9pay\n"
            "Способ получения: E9pay number\n"
            f"Номер E9pay: {os.getenv('PAY_E9PAY_NUMBER', '').strip()}\n"
            f"Получатель: {os.getenv('PAY_E9PAY_NAME', '').strip()}"
        ),
        ("PAY_E9PAY_NUMBER", "PAY_E9PAY_NAME"),
    ),
    "wu": (
        "🌍 Western Union / MoneyGram",
        (
            "🌍 Western Union / MoneyGram\n"
            f"Фамилия: {os.getenv('PAY_TRANSFER_LAST_NAME', '').strip()}\n"
            f"Имя: {os.getenv('PAY_TRANSFER_FIRST_NAME', '').strip()}\n"
            f"Телефон: {os.getenv('PAY_TRANSFER_PHONE', '').strip()}\n"
            f"Город: {os.getenv('PAY_TRANSFER_CITY', '').strip()}\n"
            "Страна: Южная Корея"
        ),
        (
            "PAY_TRANSFER_LAST_NAME", "PAY_TRANSFER_FIRST_NAME",
            "PAY_TRANSFER_PHONE", "PAY_TRANSFER_CITY",
        ),
    ),
    "pp": (
        "💳 PayPal",
        f"💳 PayPal\nEmail для получения оплаты: {os.getenv('PAY_PAYPAL_EMAIL', '').strip()}",
        ("PAY_PAYPAL_EMAIL",),
    ),
}


def payment_ready(method: str) -> bool:
    """Нельзя показывать клиентам незаполненные реквизиты."""
    return method in PAYMENT_METHODS and all(
        os.getenv(key, "").strip() for key in PAYMENT_METHODS[method][2]
    )


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=PAYMENT_METHODS[method][0], callback_data=f"pay:{order_id}:{method}")]
        for method in PAYMENT_METHODS
        if payment_ready(method)
    ]
    rows.append([InlineKeyboardButton(text="💬 Связаться с менеджером", callback_data=f"operator:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Данные об оплате хранятся в той же SQLite, что и заказы, и переживают перезапуск.
dox.db.execute(
    """CREATE TABLE IF NOT EXISTS payment_choices (
         order_id INTEGER PRIMARY KEY,
         method TEXT NOT NULL,
         customer_claimed_at TEXT
       )"""
)
dox.db.commit()


# Подменяем только обработчик подтверждения заказа. Остальные обработчики bot.py
# (включая разделение групп креаторов) сохраняются без изменений.
_old_confirm_handlers = [
    handler for handler in dox.dp.callback_query.handlers
    if handler.callback is dox.order_confirm
]
if len(_old_confirm_handlers) != 1:
    raise RuntimeError("Не найден обработчик order_confirm в bot.py; запустить без проверки нельзя")
dox.dp.callback_query.handlers.remove(_old_confirm_handlers[0])


async def order_confirm_and_payment(callback: CallbackQuery, state: FSMContext):
    before = dox.db.execute(
        "SELECT MAX(id) FROM orders WHERE client_id = ?", (callback.from_user.id,)
    ).fetchone()[0]

    # Прежняя логика: сохраняет заказ, направляет его в нужную группу креатора,
    # отвечает клиенту и завершает FSM.
    await dox.order_confirm(callback, state)

    after = dox.db.execute(
        "SELECT MAX(id) FROM orders WHERE client_id = ?", (callback.from_user.id,)
    ).fetchone()[0]
    if after is None or after == before:
        return

    if not any(payment_ready(method) for method in PAYMENT_METHODS):
        await callback.message.answer(
            "Способы оплаты пока настраиваются. Менеджер сообщит реквизиты в этом чате."
        )
        return

    await callback.message.answer(
        f"💳 Заказ №{after} оформлен. Выберите способ оплаты ниже.\n\n"
        "ВАЖНО: сумму и валюту перевода сначала подтвердите с менеджером. "
        "Не переводите деньги, пока сумма не согласована.",
        reply_markup=payment_keyboard(after),
    )


dox.dp.callback_query.register(
    order_confirm_and_payment, F.data == "order_ok", dox.OrderForm.confirm
)


def owned_order(order_id: int, user_id: int):
    """Клиент может смотреть и подтверждать оплату только своего заказа."""
    return dox.db.execute(
        "SELECT client_id, creator_code, status FROM orders WHERE id = ? AND client_id = ?",
        (order_id, user_id),
    ).fetchone()


@dox.dp.callback_query(F.data.startswith("pay:"))
async def payment_selected(callback: CallbackQuery):
    try:
        _, order_str, method = callback.data.split(":", 2)
        order_id = int(order_str)
    except (ValueError, AttributeError):
        await callback.answer("Неправильная кнопка", show_alert=True)
        return

    row = owned_order(order_id, callback.from_user.id)
    if not row or not payment_ready(method):
        await callback.answer("Заказ или способ оплаты недоступен", show_alert=True)
        return
    if row[2] == "cancelled":
        await callback.answer("Заказ отменён", show_alert=True)
        return

    dox.db.execute(
        """INSERT INTO payment_choices(order_id, method, customer_claimed_at)
           VALUES(?, ?, NULL)
           ON CONFLICT(order_id) DO UPDATE SET method = excluded.method,
             customer_claimed_at = NULL""",
        (order_id, method),
    )
    dox.db.commit()

    await callback.message.answer(
        f"Оплата заказа №{order_id}\n\n{PAYMENT_METHODS[method][1]}\n\n"
        "Перед переводом согласуйте сумму и валюту с менеджером.\n"
        "После перевода нажмите «Я оплатил», затем отправьте в этот чат "
        "квитанцию / скриншот или номер перевода.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paydone:{order_id}")],
            [InlineKeyboardButton(text="🔄 Выбрать другой способ", callback_data=f"paymethods:{order_id}")],
        ]),
    )
    await callback.answer()


@dox.dp.callback_query(F.data.startswith("paymethods:"))
async def payment_change(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer()
        return
    if not owned_order(order_id, callback.from_user.id):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.message.answer("Выберите способ оплаты:", reply_markup=payment_keyboard(order_id))
    await callback.answer()


@dox.dp.callback_query(F.data.startswith("paydone:"))
async def customer_payment_claim(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer()
        return

    order = owned_order(order_id, callback.from_user.id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order[2] in ("cancelled", "shipped"):
        await callback.answer("Оплата этого заказа больше не ожидается", show_alert=True)
        return
    if order[2] == "paid":
        await callback.answer("Менеджер уже подтвердил оплату", show_alert=True)
        return

    payment = dox.db.execute(
        "SELECT method, customer_claimed_at FROM payment_choices WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if not payment:
        await callback.answer("Сначала выберите способ оплаты", show_alert=True)
        return
    if payment[1]:
        await callback.answer("Уведомление уже отправлено менеджеру", show_alert=True)
        return

    creator_row = dox.db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (order[1],)
    ).fetchone()
    if not creator_row or not creator_row[0]:
        await callback.answer("Группа менеджера недоступна", show_alert=True)
        return

    text = (
        f"💳 Клиент сообщил об оплате заказа №{order_id}\n"
        f"Креатор: {order[1]}\n"
        f"Способ: {PAYMENT_METHODS[payment[0]][0]}\n"
        "⚠️ Поступление денег НЕ подтверждено. Проверьте перевод перед нажатием «Оплачен»."
    )
    try:
        notification = await dox.bot.send_message(
            creator_row[0], text, reply_markup=dox._order_status_keyboard(order_id)
        )
    except TelegramAPIError as exc:
        logging.error("Не удалось уведомить группу об оплате заказа %s: %s",
                      order_id, dox._safe_err(exc))
        await callback.answer("Не удалось уведомить менеджера. Попробуйте позже", show_alert=True)
        return

    dox.db.execute(
        "UPDATE payment_choices SET customer_claimed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
        (order_id,),
    )
    # Менеджер может ответить на уведомление, и ответ придёт этому клиенту.
    dox.db.execute(
        "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) VALUES(?,?,?)",
        (creator_row[0], notification.message_id, callback.from_user.id),
    )
    dox.db.commit()

    await callback.message.answer(
        "✅ Сообщение об оплате отправлено менеджеру.\n"
        "Теперь пришлите в этот чат квитанцию, скриншот или номер перевода.\n"
        "Заказ получит статус «Оплачен» после проверки поступления денег."
    )
    await callback.answer()


if __name__ == "__main__":
    asyncio.run(dox.main())
