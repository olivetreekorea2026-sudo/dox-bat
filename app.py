"""DOX Global Telegram bot launcher.

Preserves the existing bot.py order, creator-group and status logic.
Removes separate accessory sales and displays all three payment methods
in one message immediately after a customer confirms an order.

Sensitive payment details and BOT_TOKEN belong in a private server .env,
never in the public GitHub repository.
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
    raise SystemExit("BOT_TOKEN отсутствует: заполните приватный файл .env на сервере.")

os.environ.setdefault("DB_PATH", str(BASE_DIR / "bot.db"))

# Importing bot.py registers the original aiogram handlers without starting polling.
import bot as dox  # noqa: E402
from aiogram import F  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402


# Remove the separate accessories ordering steps (gifts remain part of sets).
_removed_accessory_handlers = (
    dox.item_done,
    dox.acc_pick,
    dox.acc_qty_button,
    dox.acc_done,
    dox.order_back_from_accessories,
    dox.order_back_from_acc_qty,
    dox.order_back_from_confirm,
)
for _handler in tuple(dox.dp.callback_query.handlers):
    if _handler.callback in _removed_accessory_handlers:
        dox.dp.callback_query.handlers.remove(_handler)

for _handler in tuple(dox.dp.message.handlers):
    if _handler.callback is dox.acc_qty_text:
        dox.dp.message.handlers.remove(_handler)


async def item_done_no_accessories(callback: CallbackQuery, state: FSMContext):
    await state.update_data(accessories={}, acc_current=None)
    await callback.message.edit_reply_markup()
    await dox._show_summary(callback.message, state)
    await callback.answer()


async def back_from_confirm_no_accessories(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup()
    await dox._show_more_prompt(callback.message, state)
    await callback.answer()


dox.dp.callback_query.register(
    item_done_no_accessories, F.data == "item_done", dox.OrderForm.more
)
dox.dp.callback_query.register(
    back_from_confirm_no_accessories,
    F.data == "order_back", dox.OrderForm.confirm
)


# Payment details: configured only in the private .env on Silly Development.
REQUIRED_PAYMENT_KEYS = (
    "PAY_E9PAY_NUMBER",
    "PAY_E9PAY_NAME",
    "PAY_TRANSFER_LAST_NAME",
    "PAY_TRANSFER_FIRST_NAME",
    "PAY_TRANSFER_PHONE",
    "PAY_TRANSFER_CITY",
    "PAY_PAYPAL_EMAIL",
)


def payments_configured() -> bool:
    return all(os.getenv(key, "").strip() for key in REQUIRED_PAYMENT_KEYS)


def payment_text(order_id: int) -> str:
    """Show the THREE requested methods together, without a selection step."""
    def value(key: str) -> str:
        return os.environ[key].strip()

    return (
        f"✅ Заказ №{order_id} подтверждён!\n\n"
        "💳 Реквизиты оплаты DOX Global\n\n"
        "1️⃣ Сбербанк / E9pay\n"
        "Способ получения: E9pay number\n"
        f"Номер E9pay: {value('PAY_E9PAY_NUMBER')}\n"
        f"Получатель: {value('PAY_E9PAY_NAME')}\n\n"
        "2️⃣ Western Union / MoneyGram\n"
        f"Фамилия: {value('PAY_TRANSFER_LAST_NAME')}\n"
        f"Имя: {value('PAY_TRANSFER_FIRST_NAME')}\n"
        f"Телефон: {value('PAY_TRANSFER_PHONE')}\n"
        f"Город: {value('PAY_TRANSFER_CITY')}\n"
        "Страна: South Korea\n\n"
        "3️⃣ PayPal\n"
        f"Email получателя: {value('PAY_PAYPAL_EMAIL')}\n\n"
        "🎁 Подарки в выбранном сете и доставка — бесплатно.\n"
        "⚠️ Сумму и валюту платежа необходимо подтвердить у менеджера до перевода.\n\n"
        "После перевода нажмите «Я оплатил» и отправьте сюда квитанцию "
        "или номер перевода. Менеджер проверит поступление средств."
    )


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paydone:{order_id}")],
        [InlineKeyboardButton(text="💬 Связаться с менеджером", callback_data=f"operator:{order_id}")],
    ])


# Reuse the existing payments table, including any orders from older versions.
dox.db.execute(
    """CREATE TABLE IF NOT EXISTS payment_choices (
         order_id INTEGER PRIMARY KEY,
         method TEXT NOT NULL,
         customer_claimed_at TEXT
       )"""
)
dox.db.commit()


# Replace only the order confirmation handler: the original one continues to
# create the order, notify the correct creator group and clear the order form.
_old_confirm_handlers = [
    h for h in dox.dp.callback_query.handlers if h.callback is dox.order_confirm
]
if len(_old_confirm_handlers) != 1:
    raise RuntimeError("В bot.py не найден единственный обработчик order_confirm")
dox.dp.callback_query.handlers.remove(_old_confirm_handlers[0])


async def order_confirm_and_show_payments(callback: CallbackQuery, state: FSMContext):
    previous_order_id = dox.db.execute(
        "SELECT MAX(id) FROM orders WHERE client_id = ?", (callback.from_user.id,)
    ).fetchone()[0]

    # Original bot.py safely stores the order and sends it to CREATOR01/CREATOR02.
    await dox.order_confirm(callback, state)

    new_order_id = dox.db.execute(
        "SELECT MAX(id) FROM orders WHERE client_id = ?", (callback.from_user.id,)
    ).fetchone()[0]
    if new_order_id is None or new_order_id == previous_order_id:
        return

    if not payments_configured():
        logging.warning(
            "Для оплаты не заполнены поля .env: %s",
            ", ".join(k for k in REQUIRED_PAYMENT_KEYS if not os.getenv(k, "").strip()),
        )
        await callback.message.answer(
            "✅ Заказ подтверждён. Реквизиты пока недоступны. "
            "Свяжитесь с менеджером перед оплатой.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💬 Связаться с менеджером", callback_data=f"operator:{new_order_id}"
                )]
            ]),
        )
        return

    # All three payment methods are presented together, so no method is selected.
    dox.db.execute(
        """INSERT INTO payment_choices(order_id, method, customer_claimed_at)
           VALUES(?, 'not_selected', NULL)
           ON CONFLICT(order_id) DO UPDATE SET method = 'not_selected',
             customer_claimed_at = NULL""",
        (new_order_id,),
    )
    dox.db.commit()
    await callback.message.answer(
        payment_text(new_order_id), reply_markup=payment_keyboard(new_order_id)
    )


dox.dp.callback_query.register(
    order_confirm_and_show_payments,
    F.data == "order_ok",
    dox.OrderForm.confirm,
)


def owned_order(order_id: int, user_id: int):
    return dox.db.execute(
        "SELECT client_id, creator_code, status FROM orders WHERE id = ? AND client_id = ?",
        (order_id, user_id),
    ).fetchone()


@dox.dp.callback_query(F.data.startswith("paydone:"))
async def customer_payment_claim(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Неправильная кнопка", show_alert=True)
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
        await callback.answer("Реквизиты для этого заказа не выдавались", show_alert=True)
        return
    if payment[1]:
        await callback.answer("Менеджер уже получил уведомление", show_alert=True)
        return

    creator = dox.db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (order[1],)
    ).fetchone()
    if not creator or not creator[0]:
        await callback.answer("Группа менеджера недоступна", show_alert=True)
        return

    notification_text = (
        f"💳 Клиент сообщил об оплате заказа №{order_id}\n"
        f"Креатор: {order[1]}\n"
        "Способ: E9pay / Western Union / MoneyGram / PayPal (не уточнён).\n"
        "⚠️ Это НЕ подтверждение получения денег. "
        "Проверьте платёж перед статусом «Оплачен»."
    )
    try:
        sent = await dox.bot.send_message(
            creator[0], notification_text,
            reply_markup=dox._order_status_keyboard(order_id),
        )
    except TelegramAPIError as exc:
        logging.error(
            "Не удалось уведомить менеджера по заказу %s: %s",
            order_id, dox._safe_err(exc),
        )
        await callback.answer("Не удалось уведомить менеджера. Повторите позже", show_alert=True)
        return

    dox.db.execute(
        "UPDATE payment_choices SET customer_claimed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
        (order_id,),
    )
    dox.db.execute(
        "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) VALUES (?, ?, ?)",
        (creator[0], sent.message_id, callback.from_user.id),
    )
    dox.db.commit()

    await callback.message.answer(
        "✅ Уведомление об оплате отправлено менеджеру.\n"
        "Пришлите сюда квитанцию, скриншот или номер перевода.\n"
        "Статус «Оплачен» появится после проверки поступления денег."
    )
    await callback.answer()


if __name__ == "__main__":
    asyncio.run(dox.main())
