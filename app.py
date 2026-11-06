"""DOX Global Telegram bot launcher.

Preserves the existing bot.py order, creator-group and status logic.
Requires one of two sets (2 or 3 lens pairs), prices each pair in USD,
includes free gifts/shipping, shows all payment methods after confirmation,
and requests a photo receipt for manual verification by the manager.

Sensitive payment details and BOT_TOKEN belong in a private server .env,
never in the public GitHub repository.
"""

import asyncio
import logging
import os
import re
from contextvars import ContextVar
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
from aiogram.filters import StateFilter  # noqa: E402
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message  # noqa: E402


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
    # Old inline buttons must never bypass the mandatory set size.
    data = await state.get_data()
    needed = SET_COUNTS.get(data.get("set_code"))
    if needed is None or len(data.get("items", [])) != needed:
        await callback.answer("Сначала выберите все пары линз для сета.", show_alert=True)
        return
    await state.update_data(accessories={}, acc_current=None)
    await callback.message.edit_reply_markup()
    await dox._show_summary(callback.message, state)
    await callback.answer()


async def back_from_confirm_no_accessories(callback: CallbackQuery, state: FSMContext):
    # Return to the last pair's model instead of looping to the summary.
    await state.set_state(dox.OrderForm.more)
    await dox.order_back_from_more(callback, state)


dox.dp.callback_query.register(
    item_done_no_accessories, F.data == "item_done", dox.OrderForm.more
)
dox.dp.callback_query.register(
    back_from_confirm_no_accessories,
    F.data == "order_back", dox.OrderForm.confirm
)


# ---------- Mandatory sets and USD prices ----------
# The original bot.py remains unmodified: this launcher intercepts its order
# entry and summary functions, and keeps its creator routing / statuses.
SET_COUNTS = {"SET 1": 2, "SET 2": 3}
SET_GIFTS = {
    "SET 1": ("1 раствор 150 мл", "2 дорожных набора"),
    "SET 2": ("2 раствора по 150 мл", "3 дорожных набора", "1 солнцезащитный крем"),
}
_confirming_set = ContextVar("confirming_set", default=None)


def unit_price_usd(item: dict) -> int:
    """Price for one pair, with strict handling of premium names."""
    model = re.sub(r"\s+", " ", (item.get("model") or "").upper().strip())
    if not model:
        raise ValueError("Укажите точное название модели линз.")
    # Aura variants always cost $35. Match the model name, not a broad category.
    if re.match(r"^AURA\s+(BLUE|GRAY|OLIVE|CHOCO|BROWN)(?:\b|$)", model):
        return 35
    if model == "AURA":
        raise ValueError("Уточните цвет Aura: Blue, Gray, Olive, Choco или Brown.")
    # Accept 3/03 and 5/05 spelling for the same Iris model.
    if re.match(r"^IRIS\s*0?[35](?:\b|$)", model):
        return 35
    if model == "IRIS":
        raise ValueError("Укажите номер Iris (например, Iris 03).")
    if re.match(r"^SCLERA\s*(17\.0+|22\.0+)(?=\s|$)", model):
        return 55
    if model == "SCLERA":
        raise ValueError("Укажите размер Sclera: 17.0 или 22.0.")
    return 33


def set_receipt_text(set_code: str, items: list, prices: list) -> str:
    gifts = "\n".join(f"🎁 {x} — бесплатно" for x in SET_GIFTS[set_code])
    lines = [f"🛍 DOX {set_code}"]
    for i, (item, price) in enumerate(zip(items, prices), 1):
        lines.append(
            f"{i}. {item.get('model', '').strip()} | {item.get('color', '')} — {price} $\n"
            f"    Диоптрии: L {item.get('left_eye', '')} / R {item.get('right_eye', '')}"
        )
    lines.extend((gifts, "🚚 Доставка — бесплатно", f"💵 ИТОГО: {sum(prices)} USD"))
    return "\n".join(lines)


def _set_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 SET 1 — 2 пары (от 66 $)", callback_data="doxset:1")],
        [InlineKeyboardButton(text="🎁 SET 2 — 3 пары (от 99 $)", callback_data="doxset:2")],
    ])


async def show_set_choice(message: Message):
    await message.answer(
        "DOX Global — выберите набор:\n\n"
        "🛍 SET 1: 2 пары линз + 1 раствор 150 мл + 2 дорожных набора.\n"
        "🎁 SET 2: 3 пары линз + 2 раствора по 150 мл + 3 дорожных набора "
        "+ солнцезащитный крем.\n\n"
        "✅ Подарки и доставка — бесплатно.\n"
        "Стоимость зависит только от выбранных линз. Заказать 1 пару отдельно нельзя.",
        reply_markup=_set_keyboard(),
    )


# Replace /order to require a set choice *before* collecting personal data.
for _handler in tuple(dox.dp.message.handlers):
    if _handler.callback is dox.start_order:
        dox.dp.message.handlers.remove(_handler)


async def start_order_with_sets(message: Message, state: FSMContext):
    if not dox._client_creator_code(message.from_user.id):
        await message.answer("Откройте персональную ссылку CREATOR01 или CREATOR02 для заказа.")
        return
    await state.clear()
    await show_set_choice(message)


dox.dp.message.register(start_order_with_sets, dox.Command("order"), F.chat.type == "private")
# A catch-all client-message handler was registered earlier; /order must run first.
_order_handler = dox.dp.message.handlers[-1]
dox.dp.message.handlers.remove(_order_handler)
dox.dp.message.handlers.insert(0, _order_handler)


@dox.dp.callback_query(F.data.startswith("doxset:"))
async def choose_set(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте бота в личном чате.", show_alert=True)
        return
    if not dox._client_creator_code(callback.from_user.id):
        await callback.answer("Откройте личную ссылку своего креатора.", show_alert=True)
        return
    set_code = {"doxset:1": "SET 1", "doxset:2": "SET 2"}.get(callback.data)
    if not set_code:
        await callback.answer("Неправильный сет", show_alert=True)
        return
    await state.set_data({"set_code": set_code, "step": 0, "items": [], "accessories": {}})
    await state.set_state(dox.STEP_ORDER[0])
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        f"Выбран {set_code}: {SET_COUNTS[set_code]} пары линз.\n"
        "В любой момент можно написать /cancel.\n\n"
        + dox.STEP_PROMPTS[dox.STEP_ORDER[0]]
    )
    await callback.answer()


async def show_more_for_exact_set(message: Message, state: FSMContext):
    """Called by the original _finish_item after every completed lens pair."""
    data = await state.get_data()
    set_code = data.get("set_code")
    items = data.get("items", [])
    needed = SET_COUNTS.get(set_code)
    if needed is None:
        await state.clear()
        await message.answer("Сначала выберите SET 1 или SET 2 с помощью /order.")
        return
    if len(items) > needed:
        await state.clear()
        await message.answer("В сете слишком много пар. Начните снова: /order.")
        return
    if len(items) == needed:
        await dox._show_summary(message, state)
        return
    await state.set_state(dox.OrderForm.more)
    await message.answer(
        f"✅ Пара {len(items)} из {needed} добавлена.\n"
        f"Осталось выбрать: {needed - len(items)}.\n"
        "Для этого сета нужно выбрать все пары.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Выбрать следующую пару", callback_data="item_add")],
            [InlineKeyboardButton(text="◀️ Изменить последнюю пару", callback_data="order_back")],
        ]),
    )


dox._show_more_prompt = show_more_for_exact_set


# Protect the item_add handler against old buttons or unexpected state.
for _handler in tuple(dox.dp.callback_query.handlers):
    if _handler.callback is dox.item_add:
        dox.dp.callback_query.handlers.remove(_handler)


@dox.dp.callback_query(F.data == "item_add", dox.OrderForm.more)
async def add_next_pair(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    needed = SET_COUNTS.get(data.get("set_code"))
    if needed is None or len(data.get("items", [])) >= needed:
        await callback.answer("Вы уже выбрали все пары в сете.", show_alert=True)
        return
    await dox.item_add(callback, state)


# Redo must restart at the set selector, not bypass the two-/three-pair rule.
for _handler in tuple(dox.dp.callback_query.handlers):
    if _handler.callback is dox.order_redo:
        dox.dp.callback_query.handlers.remove(_handler)


@dox.dp.callback_query(F.data == "order_redo", dox.OrderForm.confirm)
async def redo_choose_set(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup()
    await show_set_choice(callback.message)
    await callback.answer()


async def show_set_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    set_code = data.get("set_code")
    items = data.get("items", [])
    needed = SET_COUNTS.get(set_code)
    if needed is None or len(items) != needed:
        await message.answer("Для оформления выберите полный сет. Начните заново: /order.")
        return
    try:
        prices = [unit_price_usd(item) for item in items]
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nИсправьте модель последней пары кнопкой «Назад».")
        await state.set_state(dox.OrderForm.more)
        await message.answer("Измените название модели:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к модели", callback_data="order_back")],
        ]))
        return
    summary = (
        "Проверьте, пожалуйста, заказ:\n\n"
        f"Имя: {data.get('full_name', '')}\n"
        f"Адрес: {data.get('address', '')}\n"
        f"Телефон: {data.get('phone', '')}\n\n"
        + set_receipt_text(set_code, items, prices)
    )
    await state.set_state(dox.OrderForm.confirm)
    await message.answer(dox._clip(summary), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить заказ", callback_data="order_ok")],
        [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="order_redo")],
        [InlineKeyboardButton(text="◀️ Изменить последнюю пару", callback_data="order_back")],
    ]))


dox._show_summary = show_set_summary


# Price snapshots are durable and do not modify legacy order tables.
dox.db.executescript("""
CREATE TABLE IF NOT EXISTS order_sets (
    order_id INTEGER PRIMARY KEY,
    set_code TEXT NOT NULL,
    total_usd INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS order_set_prices (
    order_id INTEGER NOT NULL,
    pair_no INTEGER NOT NULL,
    price_usd INTEGER NOT NULL,
    PRIMARY KEY(order_id, pair_no)
);
""")
dox.db.commit()

_original_order_card_text = dox._order_card_text


def _order_card_with_set(order_id, data, code, status, items, accessories=None):
    old = _original_order_card_text(order_id, data, code, status, items, {})
    row = dox.db.execute(
        "SELECT set_code,total_usd FROM order_sets WHERE order_id=?", (order_id,)
    ).fetchone()
    context = _confirming_set.get()
    if row:
        set_code, total = row
        prices = [r[0] for r in dox.db.execute(
            "SELECT price_usd FROM order_set_prices WHERE order_id=? ORDER BY pair_no", (order_id,)
        ).fetchall()]
    elif context:
        set_code, total, prices = context
    else:
        return old  # A legacy order without a set.
    lines = [f"\n🛍 {set_code} — {total} USD"]
    for i, p in enumerate(prices, 1):
        lines.append(f"Пара {i}: {p} $")
    lines += [f"🎁 {gift}" for gift in SET_GIFTS[set_code]]
    lines.append("🚚 Доставка: бесплатно")
    return dox._clip(old + "\n".join(lines))


dox._order_card_text = _order_card_with_set


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
    """Display the precise USD price and THREE payment methods together."""
    def value(key: str) -> str:
        return os.environ[key].strip()

    row = dox.db.execute(
        "SELECT set_code, total_usd FROM order_sets WHERE order_id=?", (order_id,)
    ).fetchone()
    amount = (
        f"🛍 {row[0]} — к оплате: {row[1]} USD\n"
        if row else "⚠️ Итоговую сумму уточните у менеджера.\n"
    )
    return (
        f"✅ Заказ №{order_id} подтверждён!\n"
        + amount +
        "🎁 Подарки включены, доставка бесплатная.\n\n"
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
        "Стоимость заказа указана в USD. Если сервис принимает другую валюту, "
        "согласуйте курс и сумму перевода с менеджером заранее. "
        "После перевода нажмите «📷 Прикрепить скриншот оплаты». "
        "Менеджер проверит поступление средств."
    )


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Прикрепить скриншот оплаты", callback_data=f"proof:{order_id}")],
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

# An unfinished request to submit a receipt survives a bot restart.
dox.db.execute(
    """CREATE TABLE IF NOT EXISTS receipt_pending (
        user_id INTEGER PRIMARY KEY,
        order_id INTEGER NOT NULL,
        requested_at TEXT DEFAULT CURRENT_TIMESTAMP
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

    # Validate price and set size *before* writing an order to the database.
    data = await state.get_data()
    set_code = data.get("set_code")
    items = data.get("items", [])
    if set_code not in SET_COUNTS or len(items) != SET_COUNTS[set_code]:
        await callback.answer("Для покупки выберите полный SET 1 или SET 2.", show_alert=True)
        return
    try:
        prices = [unit_price_usd(it) for it in items]
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    total = sum(prices)
    # The original order sender uses the same coroutine context while constructing
    # the initial creator-group notification, before set metadata is persisted.
    token = _confirming_set.set((set_code, total, prices))
    try:
        await dox.order_confirm(callback, state)
    finally:
        _confirming_set.reset(token)

    new_order_id = dox.db.execute(
        "SELECT MAX(id) FROM orders WHERE client_id = ?", (callback.from_user.id,)
    ).fetchone()[0]
    if new_order_id is None or new_order_id == previous_order_id:
        return

    # Snapshot the set and USD pricing for this order (future price changes
    # must not silently change earlier customers' totals).
    dox.db.execute(
        "INSERT INTO order_sets(order_id,set_code,total_usd) VALUES(?,?,?)",
        (new_order_id, set_code, total),
    )
    dox.db.executemany(
        "INSERT INTO order_set_prices(order_id,pair_no,price_usd) VALUES(?,?,?)",
        [(new_order_id, i + 1, price) for i, price in enumerate(prices)],
    )
    dox.db.commit()

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


@dox.dp.callback_query(F.data.startswith("proof:"))
async def request_receipt_photo(callback: CallbackQuery):
    """Request an actual image instead of accepting an unverified 'I paid' click."""
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Неправильная кнопка", show_alert=True)
        return

    order = owned_order(order_id, callback.from_user.id)
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order[2] in ("cancelled", "shipped", "paid"):
        await callback.answer("Для этого заказа загрузка чека закрыта", show_alert=True)
        return

    payment = dox.db.execute(
        "SELECT 1 FROM payment_choices WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not payment:
        await callback.answer("Сначала получите реквизиты оплаты", show_alert=True)
        return

    dox.db.execute(
        """INSERT INTO receipt_pending(user_id, order_id, requested_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(user_id) DO UPDATE
           SET order_id=excluded.order_id, requested_at=CURRENT_TIMESTAMP""",
        (callback.from_user.id, order_id),
    )
    dox.db.commit()

    await callback.message.answer(
        f"📷 Заказ №{order_id}: прикрепите скриншот оплаты.\n\n"
        "Нажмите 📎 (или +) в Telegram и отправьте фото чека в этот чат. "
        "Можно отправить скриншот изображением, как фото или файлом JPG/PNG.\n\n"
        "Чек будет отправлен менеджеру вашего креатора. "
        "Не присылайте пароли и коды подтверждения.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Отменить загрузку чека", callback_data=f"proofcancel:{order_id}"
        )]]),
    )
    await callback.answer()


@dox.dp.callback_query(F.data.startswith("proofcancel:"))
async def cancel_receipt_photo(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Неверная кнопка", show_alert=True)
        return
    dox.db.execute(
        "DELETE FROM receipt_pending WHERE user_id=? AND order_id=?",
        (callback.from_user.id, order_id),
    )
    dox.db.commit()
    await callback.message.answer("Загрузка скриншота отменена. Чтобы отправить его, нажмите кнопку под реквизитами снова.")
    await callback.answer()


# The original private message handler forwards free-form messages to the creator.
# Replace it with a wrapper so receipt images get the order number and status.
_old_client_handlers = [
    handler for handler in dox.dp.message.handlers
    if handler.callback is dox.from_client
]
if len(_old_client_handlers) != 1:
    raise RuntimeError("Не найден обработчик сообщений клиента в bot.py")
dox.dp.message.handlers.remove(_old_client_handlers[0])


def _image_receipt(message: Message) -> bool:
    if message.photo:
        return True
    if message.document:
        mime_type = (message.document.mime_type or "").lower()
        file_name = (message.document.file_name or "").lower()
        return mime_type.startswith("image/") or file_name.endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        )
    return False


@dox.dp.message(F.chat.type == "private", StateFilter(None))
async def receipt_photo_or_regular_message(message: Message):
    pending = dox.db.execute(
        "SELECT order_id FROM receipt_pending WHERE user_id=?", (message.from_user.id,)
    ).fetchone()
    if not pending:
        # Preserve the original free-form messages and photo forwarding.
        await dox.from_client(message)
        return

    order_id = pending[0]
    order = owned_order(order_id, message.from_user.id)
    if not order or order[2] in ("cancelled", "shipped", "paid"):
        dox.db.execute("DELETE FROM receipt_pending WHERE user_id=?", (message.from_user.id,))
        dox.db.commit()
        await message.answer("Загрузка чека для этого заказа больше недоступна.")
        return

    if not _image_receipt(message):
        await message.answer(
            f"📷 Для заказа №{order_id} отправьте именно ФОТО или скриншот "
            "в формате JPG/PNG. Текст не считается подтверждением оплаты. "
            "Если хотите отменить загрузку, нажмите «Отменить загрузку чека» выше."
        )
        return

    creator = dox.db.execute(
        "SELECT group_id FROM creators WHERE code=?", (order[1],)
    ).fetchone()
    if not creator or not creator[0]:
        await message.answer("Группа менеджера недоступна. Сохраните чек и повторите попытку позже.")
        return

    group_id = creator[0]
    header_text = (
        f"📷 Получен скриншот оплаты заказа №{order_id}\n"
        f"Креатор: {order[1]}\n"
        f"Клиент: {message.from_user.full_name} (ID: {message.from_user.id})\n\n"
        "⚠️ Чек отправлен клиентом. Это НЕ подтверждение поступления денег. "
        "Менеджер должен проверить перевод перед нажатием «Оплачен»."
    )
    try:
        # A group header + native Telegram photo lets staff inspect the receipt.
        header = await dox.bot.send_message(
            group_id, header_text,
            reply_markup=dox._order_status_keyboard(order_id),
        )
        photo = await dox.bot.copy_message(
            group_id, message.chat.id, message.message_id,
            reply_to_message_id=header.message_id,
        )
    except TelegramAPIError as exc:
        logging.error(
            "Не удалось отправить скриншот оплаты по заказу %s: %s",
            order_id, dox._safe_err(exc),
        )
        await message.answer("Не удалось передать скриншот менеджеру. Повторите отправку фото позже.")
        return

    dox.db.execute(
        "UPDATE payment_choices SET customer_claimed_at=CURRENT_TIMESTAMP WHERE order_id=?",
        (order_id,),
    )
    for msg_id in (header.message_id, photo.message_id):
        dox.db.execute(
            "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) VALUES(?, ?, ?)",
            (group_id, msg_id, message.from_user.id),
        )
    dox.db.execute("DELETE FROM receipt_pending WHERE user_id=?", (message.from_user.id,))
    dox.db.commit()
    await message.answer(
        f"✅ Скриншот оплаты заказа №{order_id} отправлен менеджеру.\n"
        "После проверки поступления средств менеджер подтвердит оплату."
    )


# ---------- Уведомления клиенту о подтверждённой оплате и отправке ----------
# Фото платежа само по себе НЕ переводит заказ в статус "Оплачен".
# Статусы меняет только администратор группы конкретного креатора.
# Уведомление отправляется не более одного раза для каждого статуса;
# при ошибке доставки повторное нажатие той же кнопки позволяет повторить попытку.
dox.db.execute(
    """CREATE TABLE IF NOT EXISTS status_notifications (
        order_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(order_id, status)
    )"""
)
dox.db.commit()

_old_status_handlers = [
    h for h in dox.dp.callback_query.handlers
    if h.callback is dox.order_status_change
]
if len(_old_status_handlers) != 1:
    raise RuntimeError("Не найден единственный обработчик статусов заказа в bot.py")
dox.dp.callback_query.handlers.remove(_old_status_handlers[0])

CUSTOMER_STATUS_MESSAGES = {
    "paid": (
        "✅ DOX Global — Оплата подтверждена!\n\n"
        "💰 Оплата по заказу №{order_id} получена и проверена менеджером.\n"
        "Спасибо за покупку! Ваш заказ готовится к отправке."
    ),
    "shipped": (
        "📦 DOX Global — Заказ отправлен!\n\n"
        "Ваш заказ №{order_id} передан в доставку.\n"
        "🚚 Доставка бесплатная.\n"
        "Спасибо, что выбрали DOX Global!"
    ),
}


def _was_status_notification_sent(order_id: int, status: str) -> bool:
    return bool(dox.db.execute(
        "SELECT 1 FROM status_notifications WHERE order_id=? AND status=?",
        (order_id, status),
    ).fetchone())


@dox.dp.callback_query(F.data.startswith("status:"))
async def status_change_with_client_notice(callback: CallbackQuery):
    try:
        prefix, order_id_text, new_status = callback.data.split(":", 2)
        order_id = int(order_id_text)
        if prefix != "status" or new_status not in ("paid", "shipped", "cancelled"):
            raise ValueError("invalid status")
    except (ValueError, AttributeError):
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    if not callback.message or callback.message.chat.type not in dox.GROUP_TYPES:
        await callback.answer("Статус меняется только в группе менеджеров", show_alert=True)
        return

    group_id = callback.message.chat.id
    order = dox.db.execute(
        "SELECT client_id, creator_code, status, full_name, address, phone, "
        "left_eye, right_eye, color, model FROM orders WHERE id=?",
        (order_id,),
    ).fetchone()
    if not order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    client_id, creator_code, old_status = order[:3]
    creator_group = dox.db.execute(
        "SELECT group_id FROM creators WHERE code=?", (creator_code,)
    ).fetchone()
    if not creator_group or creator_group[0] != group_id:
        await callback.answer("Это заказ другого креатора", show_alert=True)
        return

    try:
        member = await dox.bot.get_chat_member(group_id, callback.from_user.id)
    except TelegramAPIError as exc:
        logging.error("Проверка администратора: %s", dox._safe_err(exc))
        await callback.answer("Не удалось проверить права администратора", show_alert=True)
        return
    if member.status not in ("creator", "administrator"):
        await callback.answer("Только администратор может менять статус", show_alert=True)
        return

    if old_status == "cancelled":
        await callback.answer("Отменённый заказ нельзя подтверждать или отправлять", show_alert=True)
        return
    if old_status == "shipped" and new_status != "shipped":
        await callback.answer("Заказ уже отправлен; изменение статуса назад запрещено", show_alert=True)
        return
    if new_status == "shipped" and old_status not in ("paid", "shipped"):
        await callback.answer("Сначала подтвердите оплату кнопкой «Оплачен»", show_alert=True)
        return
    if new_status == "paid" and old_status not in ("new", "paid"):
        await callback.answer("Невозможно подтвердить оплату для этого статуса", show_alert=True)
        return

    # Для новых заказов в виде сетов подтверждение оплаты возможно только
    # после реальной загрузки фото/скриншота чека от клиента.
    if new_status == "paid" and old_status == "new":
        is_set = dox.db.execute(
            "SELECT 1 FROM order_sets WHERE order_id=?", (order_id,)
        ).fetchone()
        if is_set:
            proof = dox.db.execute(
                "SELECT customer_claimed_at FROM payment_choices WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if not proof or not proof[0]:
                await callback.answer(
                    "Сначала клиент должен прикрепить скриншот оплаты", show_alert=True
                )
                return

    if old_status != new_status:
        dox.db.execute(
            "UPDATE orders SET status=? WHERE id=?", (new_status, order_id)
        )
        dox.db.commit()
        data = {"full_name": order[3], "address": order[4], "phone": order[5]}
        items = dox._load_items(order_id, order[6:10])
        new_card = dox._order_card_text(
            order_id, data, creator_code, new_status, items,
            dox._load_accessories(order_id),
        )
        try:
            await callback.message.edit_text(
                new_card, reply_markup=dox._order_status_keyboard(order_id)
            )
        except TelegramAPIError as exc:
            logging.error(
                "Не получилось обновить карточку заказа №%s: %s",
                order_id, dox._safe_err(exc),
            )

    if new_status not in CUSTOMER_STATUS_MESSAGES:
        await callback.answer("Статус заказа изменён: Отменён")
        return

    if _was_status_notification_sent(order_id, new_status):
        await callback.answer("Статус установлен; клиент уже получил уведомление")
        return

    try:
        await dox.bot.send_message(
            client_id, CUSTOMER_STATUS_MESSAGES[new_status].format(order_id=order_id)
        )
    except TelegramAPIError as exc:
        logging.error(
            "Уведомление по заказу №%s (%s) не доставлено: %s",
            order_id, new_status, dox._safe_err(exc),
        )
        await callback.answer(
            "Статус сохранён, но клиент НЕ получил сообщение. "
            "Проверьте, не заблокирован ли бот, и нажмите кнопку повторно.",
            show_alert=True,
        )
        return

    dox.db.execute(
        "INSERT OR IGNORE INTO status_notifications(order_id,status) VALUES(?,?)",
        (order_id, new_status),
    )
    dox.db.commit()
    await callback.answer(
        "Оплата подтверждена, клиент уведомлён" if new_status == "paid"
        else "Отправка подтверждена, клиент уведомлён"
    )


if __name__ == "__main__":
    asyncio.run(dox.main())
