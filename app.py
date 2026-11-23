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


# International shipping uses the legacy address and phone fields, so the
# existing orders table and manager workflow require no migration.
# The delivery country is included in the address line rather than introducing
# a new column that could break the previously deployed bot.py.
def _set_international_shipping_prompts():
    prompts = getattr(dox, "STEP_PROMPTS", None)
    if not isinstance(prompts, dict):
        logging.warning("STEP_PROMPTS is unavailable; existing delivery prompts remain unchanged")
        return
    for state, text in tuple(prompts.items()):
        normalized = str(text).casefold()
        if "адрес" in normalized:
            prompts[state] = (
                "📍 Введите полный адрес для международной доставки латиницей, "
                "ОДНИМ сообщением:\n"
                "страна, город, улица, дом, квартира (если есть), почтовый индекс.\n\n"
                "Пример: Kazakhstan, Almaty, Abay Ave 10, Apt 5, 050000.\n"
                "Проверьте адрес перед подтверждением заказа. Если индекса нет, "
                "уточните у менеджера, возможна ли доставка без него."
            )
        elif "телефон" in normalized:
            prompts[state] = (
                "📱 Введите номер телефона получателя в международном формате "
                "с кодом страны (например, +77001234567)."
            )
        elif "имя" in normalized and ("фамил" in normalized or "полное" in normalized):
            prompts[state] = (
                "👤 Введите имя и фамилию получателя одним сообщением "
                "латинскими буквами. Пример: ANNA IVANOVA."
            )


_set_international_shipping_prompts()


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
# Internal identifiers are intentionally unchanged for DB compatibility.
# In the buyer interface, the 2-pair option has no SET number; the 3-pair
# option is called SET 1. Existing creator links and saved order keys remain valid.
SET_COUNTS = {"SET 1": 2, "SET 2": 3}
SET_PUBLIC_NAMES = {"SET 1": "Выбор 2-х пар линз", "SET 2": "SET 1"}
SET_STARTING_PRICES = {"SET 1": 66, "SET 2": 110}
PAIR_BASE_USD = 33
AURA_EXTRA_USD = 5


def public_set_name(set_code: str) -> str:
    return SET_PUBLIC_NAMES.get(set_code, set_code)


def is_aura_item(item: dict) -> bool:
    """Only the five AURA colours have a premium inside the 3-pair SET 1."""
    model = re.sub(r"\s+", " ", (item.get("model") or "").upper().strip())
    return bool(re.fullmatch(r"AURA (?:BLUE|OLIVE|CHOCO|BROWN|GRAY)", model))


def set_total_usd(set_code: str, prices: list[int], items: list[dict]) -> int:
    """2 pairs: sum model prices. 3-pair SET 1: $110 + $5 per AURA pair.

    Price of one AURA pair is always $35 when purchased in the 2-pair option.
    Prices of IRIS/SCLERA are reflected in the 2-pair total, but the 3-pair
    SET 1 has a fixed base price regardless of those selected models.
    """
    if set_code not in SET_COUNTS or len(prices) != SET_COUNTS[set_code]:
        raise ValueError("Для расчёта цены необходимо выбрать все пары набора.")
    if len(items) != len(prices):
        raise ValueError("Количество моделей и цен не совпадает.")
    if any(price < PAIR_BASE_USD for price in prices):
        raise ValueError("Некорректная цена модели.")
    if set_code == "SET 1":
        return sum(prices)
    return SET_STARTING_PRICES[set_code] + AURA_EXTRA_USD * sum(
        1 for item in items if is_aura_item(item)
    )

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
    # AURA is $35 per pair; the +$5 surcharge applies ONLY to the 3-pair SET 1.
    if is_aura_item({"model": model}):
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
    lines = [f"🛍 DOX Global — {public_set_name(set_code)}"]
    for i, (item, price) in enumerate(zip(items, prices), 1):
        # Do not suggest the individual list prices are charged on top of a set.
        unit_label = f" — {price} $" if set_code == "SET 1" else ""
        lines.append(
            f"{i}. {item.get('model', '').strip()} | {item.get('color', '')}{unit_label}\n"
            f"    Диоптрии: L {item.get('left_eye', '')} / R {item.get('right_eye', '')}"
        )
    lines.append(gifts)
    lines.append("🚚 Доставка — бесплатно")
    if set_code == "SET 1":
        # 2 selected pairs cost exactly the sum of the chosen model list prices.
        lines.append("💵 Стоимость 2 пар по выбранным моделям")
        lines.append("✨ AURA: 35 $ за пару, без дополнительной наценки")
    else:
        aura_count = sum(is_aura_item(item) for item in items)
        lines.append("💵 Стоимость SET 1 (3 пары): 110 $")
        if aura_count:
            lines.append(f"✨ Доплата за AURA: {aura_count} × 5 $ = +{aura_count * AURA_EXTRA_USD} $")
        else:
            lines.append("✨ Доплата за AURA: 0 $")
    lines.append(f"💵 ИТОГО: {set_total_usd(set_code, prices, items)} USD")
    return "\n".join(lines)

def _set_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Выбор 2-х пар линз — от 66 $", callback_data="doxset:1")],
        [InlineKeyboardButton(text="🎁 SET 1 — 3 пары — от 110 $", callback_data="doxset:2")],
    ])


async def show_set_choice(message: Message):
    await message.answer(
        "🌍 DOX Global — доставка из Южной Кореи за границу.\n\n"
        "Выберите один из двух обязательных наборов:\n"
        "🛍 Выбор 2-х пар линз: 2 пары линз + 1 раствор 150 мл + 2 дорожных набора — от 66 $.\n"
        "🎁 SET 1: 3 пары линз + 2 раствора по 150 мл + 3 дорожных набора "
        "+ солнцезащитный крем — от 110 $.\n\n"
        "✨ Хотите добавить серию AURA (BLUE, OLIVE, CHOCO, BROWN, GRAY)?\n"
        "При выборе 2 пар AURA стоит 35 $ за пару, без доплаты.\n"
        "В SET 1 за 110 $ каждая пара AURA увеличивает цену сета на 5 $.\n\n"
        "💵 При выборе 2 пар цена зависит от моделей (33 / 35 / 55 $ за пару).\n"
        "🎁 Все аксессуары в комплекте бесплатно.\n"
        "🚚 Доставка бесплатная, возможность отправки в вашу страну "
        "подтверждает менеджер.\n\n"
        "Для доставки понадобятся имя и фамилия латиницей, полный адрес "
        "со страной и городом и номер телефона с международным кодом.\n"
        "Заказ одной пары отдельно недоступен.",
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
        f"Выбран {public_set_name(set_code)}: {SET_COUNTS[set_code]} пары линз.\n"
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
        await message.answer("Сначала выберите набор с помощью /order.")
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
        f"Адрес доставки (включая страну): {data.get('address', '')}\n"
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
    lines = [f"\n🛍 {public_set_name(set_code)} — {total} USD"]
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
        f"🛍 {public_set_name(row[0])} — к оплате: {row[1]} USD\n"
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
        await callback.answer("Для покупки выберите все пары выбранного набора.", show_alert=True)
        return
    try:
        prices = [unit_price_usd(it) for it in items]
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    total = set_total_usd(set_code, prices, items)
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




# ===========================================================================
# DOX GLOBAL - KOREA-STYLE ORDER WIZARD (international version)
# ===========================================================================
# This layer changes only the buyer's order UI. The existing DOX Global
# order persistence, creator routing, payment credentials, receipt forwarding,
# and manager statuses stay in app.py / bot.py.
# It uses the same model catalogue as DOX Korea but international USD pricing.
from aiogram import BaseMiddleware  # noqa: E402
from aiogram.filters import Command as GlobalCommand  # noqa: E402
from aiogram.fsm.state import State as GlobalState, StatesGroup as GlobalStatesGroup  # noqa: E402
from aiogram.types import FSInputFile  # noqa: E402
from korea_catalog import (  # noqa: E402
    CATEGORIES as GLOBAL_CATEGORIES,
    BY_CODE as GLOBAL_MODELS,
    BY_CATEGORY as GLOBAL_BY_CATEGORY,
    normalize_diopter as global_normalize_diopter,
    diopter_hint as global_diopter_hint,
)


class GlobalOrderFlow(GlobalStatesGroup):
    category = GlobalState()
    model = GlobalState()
    od = GlobalState()
    os = GlobalState()
    full_name = GlobalState()
    phone = GlobalState()
    country = GlobalState()
    city = GlobalState()
    postal = GlobalState()
    street = GlobalState()


_GLOBAL_CATALOG_FILES = {
    "blue": "Blue.png",
    "brown": "Brown_and_Choco.jpg",
    "gray": "Gray.jpg",
    "green": "Green.png",
    "violet": "Violet.png",
    "special": "Прозрачные_и_специальные_линзы.png",
    "black": "Black.png",
}


def global_catalog_image_path(category: str) -> Path | None:
    """Find an approved catalogue image without depending on Korea assets.

    Prioritize the bundled Global catalogues, then support the original root
    layout and the pre-existing Korea image directory for older deployments.
    Never silently resolve to a different category's picture.
    """
    file_name = _GLOBAL_CATALOG_FILES.get(category)
    if not file_name:
        return None
    candidates = [
        BASE_DIR / "catalog" / file_name,          # already installed and working on Silly
        BASE_DIR / "global_catalogs" / file_name,  # alternative Global assets folder
        BASE_DIR / file_name,                       # original GitHub layout
    ]
    korea_record = GLOBAL_CATEGORIES.get(category)
    if korea_record:
        candidates.append(BASE_DIR / "korea_images" / korea_record[1])
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 1000:
            return candidate
    return None


def global_category_keyboard(set_code: str, pair_number: int):
    rows = []
    for category, (title, _filename) in GLOBAL_CATEGORIES.items():
        rows.append([InlineKeyboardButton(
            text=title, callback_data=f"gcat:{pair_number}:{category}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def global_model_keyboard(pair_number: int, category: str):
    rows = [
        [InlineKeyboardButton(text=model.name, callback_data=f"gmodel:{pair_number}:{model.code}")]
        for model in GLOBAL_BY_CATEGORY[category]
    ]
    rows.append([InlineKeyboardButton(text="◀️ К выбору цвета", callback_data=f"gback:{pair_number}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def global_current_total(items: list) -> int:
    return sum(unit_price_usd(item) for item in items)


async def global_next_pair(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", [])
    set_code = data.get("set_code")
    count = SET_COUNTS.get(set_code)
    if count is None:
        await state.clear()
        await message.answer("Выберите один из наборов: /order")
        return
    if len(items) >= count:
        await state.set_state(GlobalOrderFlow.full_name)
        await message.answer(
            "✅ Вы выбрали все пары линз.\n\n"
            "Введите ИМЯ И ФАМИЛИЮ одним сообщением латиницей.\n"
            "Пример: ANNA IVANOVA"
        )
        return
    pair_number = len(items) + 1
    await state.update_data(pair_number=pair_number, category=None, model_code=None)
    await state.set_state(GlobalOrderFlow.category)
    await message.answer(
        f"👁 Пара {pair_number} из {count}. Выберите цветовой каталог:",
        reply_markup=global_category_keyboard(set_code, pair_number),
    )


# Supersede the old SET selector's transition to recipient data first.
# The new flow collects lenses first, then recipient/delivery details.
for _handler in tuple(dox.dp.callback_query.handlers):
    if _handler.callback is choose_set:
        dox.dp.callback_query.handlers.remove(_handler)


@dox.dp.callback_query(F.data.startswith("doxset:"))
async def global_choose_set(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    creator = dox._client_creator_code(callback.from_user.id)
    if not creator:
        await callback.answer("Перейдите по ссылке вашего креатора.", show_alert=True)
        return
    selected = {"doxset:1": "SET 1", "doxset:2": "SET 2"}.get(callback.data)
    if not selected:
        await callback.answer("Неверный сет.", show_alert=True)
        return
    await state.clear()
    await state.update_data(set_code=selected, step=0, items=[], accessories={}, creator_code=creator)
    await callback.answer()
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        f"✅ Вы выбрали {public_set_name(selected)}.\n"
        f"Необходимо выбрать {SET_COUNTS[selected]} пары линз.\n"
        + ("Стоимость двух пар — сумма цен выбранных моделей.\n" if selected == "SET 1"
           else "Стоимость SET 1 — 110 $ + 5 $ за каждую выбранную пару AURA.\n")
        + "После выбора линз бот попросит данные доставки.\n"
        "Отменить заполнение: /cancel"
    )
    await global_next_pair(callback.message, state)


@dox.dp.callback_query(F.data.startswith("gcat:"))
async def global_select_category(callback: CallbackQuery, state: FSMContext):
    try:
        _, pair_str, category = callback.data.split(":")
        pair_number = int(pair_str)
    except (AttributeError, ValueError):
        await callback.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    if (await state.get_state() != GlobalOrderFlow.category.state
            or pair_number != data.get("pair_number")
            or category not in GLOBAL_CATEGORIES):
        await callback.answer("Используйте последнюю кнопку выбора цвета.", show_alert=True)
        return
    await state.update_data(category=category)
    await state.set_state(GlobalOrderFlow.model)
    title = GLOBAL_CATEGORIES[category][0]
    path = global_catalog_image_path(category)
    await callback.answer()
    photo_caption = (f"👁 Пара {pair_number} из {SET_COUNTS[data['set_code']]}. "
                     f"Каталог: {title}")
    keyboard = global_model_keyboard(pair_number, category)
    if path is not None:
        try:
            # Send the image first. The model selection is a SEPARATE message,
            # so the keyboard remains visible below even after the photo opens.
            await callback.message.answer_photo(FSInputFile(path), caption=photo_caption)
        except TelegramAPIError:
            logging.exception("DOX Global: unable to send catalogue image: %s", path)
    else:
        logging.error("DOX Global: catalogue photo missing for %s", category)
    # ALWAYS send a dedicated model keyboard, irrespective of photo status.
    # If Telegram rejects a photo upload, model selection still works.
    model_count = len(GLOBAL_BY_CATEGORY[category])
    await callback.message.answer(
        f"🌍 DOX Global — {title}\n"
        f"Выберите название модели линз (всего {model_count}) кнопкой ниже:👇",
        reply_markup=keyboard,
    )
    logging.info("DOX Global MODEL BUTTONS delivered: %s / pair %s / %s models",
                 category, pair_number, model_count)


@dox.dp.callback_query(F.data.startswith("gback:"))
async def global_back_to_category(callback: CallbackQuery, state: FSMContext):
    try:
        pair_number = int(callback.data.partition(":")[2])
    except (AttributeError, ValueError):
        await callback.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    if (await state.get_state() != GlobalOrderFlow.model.state
            or pair_number != data.get("pair_number")):
        await callback.answer("Эта кнопка больше неактуальна.", show_alert=True)
        return
    await state.set_state(GlobalOrderFlow.category)
    await callback.answer()
    await callback.message.answer(
        f"Выберите другой каталог для пары {pair_number}:",
        reply_markup=global_category_keyboard(data["set_code"], pair_number),
    )


@dox.dp.callback_query(F.data.startswith("gmodel:"))
async def global_select_model(callback: CallbackQuery, state: FSMContext):
    try:
        _, pair_str, model_code = callback.data.split(":")
        pair_number = int(pair_str)
    except (AttributeError, ValueError):
        await callback.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    model = GLOBAL_MODELS.get(model_code)
    if (await state.get_state() != GlobalOrderFlow.model.state
            or pair_number != data.get("pair_number")
            or not model or data.get("category") != model.category):
        await callback.answer("Выберите модель на последнем шаге.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(model_code=model_code)
    price = unit_price_usd({"model": model.name})
    set_code = data.get("set_code")
    price_description = (f"Цена пары: {price} $." if set_code == "SET 1" else
                         f"SET 1: 110 $ + 5 $ за каждую пару AURA (модель {model.name}).")
    if model.diopters == ("0.00",):
        await global_add_completed_pair(callback.message, state, model.name, "0.00", "0.00")
        return
    await state.set_state(GlobalOrderFlow.od)
    await callback.message.answer(
        f"👁 Пара {pair_number}: {model.name}\n{price_description}\n"
        "Введите диоптрию ПРАВОГО глаза (OD).\n"
        f"{global_diopter_hint(model)}"
    )


async def global_add_completed_pair(message: Message, state: FSMContext,
                                    model_name: str, od: str, os_: str):
    data = await state.get_data()
    items = list(data.get("items", []))
    model = GLOBAL_MODELS.get(data.get("model_code"))
    if model is None or model.name != model_name:
        await message.answer("Модель не найдена, начните заново: /order")
        await state.clear()
        return
    # Same item structure as legacy DOX Global order storage.
    items.append({
        "model": model_name,
        "color": GLOBAL_CATEGORIES[model.category][0],
        "right_eye": od,
        "left_eye": os_,
    })
    set_code = data["set_code"]
    await state.update_data(items=items, model_code=None, od=None)
    if set_code == "SET 1":
        price_message = (
            f"Цена пары: {unit_price_usd(items[-1])} $.\n"
            f"Стоимость выбранных пар: {global_current_total(items)} $."
        )
    else:
        aura_count = sum(is_aura_item(item) for item in items)
        price_message = (
            f"Базовая цена SET 1: 110 $.\n"
            f"Доплата за AURA: {aura_count} × 5 $ = {aura_count * 5} $.\n"
            f"Текущая цена SET 1: {110 + aura_count * 5} $."
        )
    await message.answer(
        f"✅ Пара {len(items)} из {SET_COUNTS[set_code]}: {model_name}.\n"
        f"Диоптрии: OD {od} / OS {os_}.\n"
        + price_message
    )
    await global_next_pair(message, state)


@dox.dp.message(GlobalOrderFlow.od, F.chat.type == "private")
async def global_od(message: Message, state: FSMContext):
    data = await state.get_data()
    model = GLOBAL_MODELS.get(data.get("model_code"))
    if model is None:
        await state.clear()
        await message.answer("Ошибка выбора модели. Начните /order")
        return
    value = global_normalize_diopter(message.text or "", model)
    if value is None:
        await message.answer(f"Уточните диоптрию OD для {model.name}.\n{global_diopter_hint(model)}")
        return
    await state.update_data(od=value)
    await state.set_state(GlobalOrderFlow.os)
    await message.answer(f"Введите диоптрию ЛЕВОГО глаза (OS) для {model.name}.\n"
                         + global_diopter_hint(model))


@dox.dp.message(GlobalOrderFlow.os, F.chat.type == "private")
async def global_os(message: Message, state: FSMContext):
    data = await state.get_data()
    model = GLOBAL_MODELS.get(data.get("model_code"))
    if model is None:
        await state.clear()
        await message.answer("Ошибка выбора модели. Начните /order")
        return
    value = global_normalize_diopter(message.text or "", model)
    if value is None:
        await message.answer(f"Уточните диоптрию OS для {model.name}.\n{global_diopter_hint(model)}")
        return
    await global_add_completed_pair(message, state, model.name, data.get("od", "0.00"), value)


def global_valid_name(value: str) -> bool:
    parts = value.strip().split()
    return (2 <= len(parts) <= 5 and len(value) <= 100 and
            all(re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", p) for p in parts))


def global_valid_phone(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("+") and 8 <= len(re.sub(r"\D", "", stripped)) <= 15 \
        and re.fullmatch(r"\+[0-9 ()-]+", stripped) is not None


def global_latin_text(value: str, min_length: int = 2) -> bool:
    # Latin address can include digits and punctuation; reject Cyrillic/Hangul.
    return (min_length <= len(value.strip()) <= 200
            and re.search(r"[A-Za-z]", value) is not None
            and re.search(r"[\u0400-\u052F\uAC00-\uD7AF]", value) is None)


@dox.dp.message(GlobalOrderFlow.full_name, F.chat.type == "private")
async def global_recipient_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not global_valid_name(name):
        await message.answer("Введите ИМЯ И ФАМИЛИЮ одним сообщением латиницей. Пример: ANNA IVANOVA")
        return
    await state.update_data(full_name=name)
    await state.set_state(GlobalOrderFlow.phone)
    await message.answer("📱 Введите номер телефона с кодом страны. Например: +491701234567")


@dox.dp.message(GlobalOrderFlow.phone, F.chat.type == "private")
async def global_recipient_phone(message: Message, state: FSMContext):
    phone = (message.text or "").strip()
    if not global_valid_phone(phone):
        await message.answer("Укажите телефон с + и кодом страны, например: +491701234567")
        return
    await state.update_data(phone=phone)
    await state.set_state(GlobalOrderFlow.country)
    await message.answer("🌍 Введите СТРАНУ доставки латиницей. Например: Germany")


@dox.dp.message(GlobalOrderFlow.country, F.chat.type == "private")
async def global_country(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not global_latin_text(value, 3):
        await message.answer("Введите страну латиницей, например Germany или Kazakhstan.")
        return
    await state.update_data(country=value)
    await state.set_state(GlobalOrderFlow.city)
    await message.answer("🏙 Введите ГОРОД доставки латиницей. Например: Berlin")


@dox.dp.message(GlobalOrderFlow.city, F.chat.type == "private")
async def global_city(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not global_latin_text(value):
        await message.answer("Введите город латиницей, например Berlin.")
        return
    await state.update_data(city=value)
    await state.set_state(GlobalOrderFlow.postal)
    await message.answer(
        "📮 Введите почтовый индекс для международной доставки.\n"
        "Если почтового индекса нет, нажмите «⏭ Пропустить индекс».\n"
        "Некоторые страны и службы доставки требуют индекс.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить индекс", callback_data="gpost:skip")]
        ]),
    )


async def global_to_street(message: Message, state: FSMContext, postal: str):
    await state.update_data(postal=postal)
    await state.set_state(GlobalOrderFlow.street)
    await message.answer(
        "📍 Введите полный адрес УЛИЦЫ, ДОМ и КВАРТИРУ (если есть) латиницей "
        "одним сообщением.\nПример: Friedrichstrasse 10, Apt 5"
    )


@dox.dp.message(GlobalOrderFlow.postal, F.chat.type == "private")
async def global_postal(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 -]{2,16}", value):
        await message.answer("Введите индекс (буквы/цифры, до 16 символов) или нажмите «Пропустить индекс».")
        return
    await global_to_street(message, state, value)


@dox.dp.callback_query(F.data == "gpost:skip")
async def global_postal_skip(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != GlobalOrderFlow.postal.state:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup()
    await global_to_street(callback.message, state, "")


async def global_show_review(message: Message, state: FSMContext):
    """Korea-style order review; keep the existing confirmation/payment backend."""
    data = await state.get_data()
    set_code = data.get("set_code")
    items = data.get("items", [])
    if set_code not in SET_COUNTS or len(items) != SET_COUNTS[set_code]:
        await state.clear()
        await message.answer("Набор заполнен не полностью. Начните заказ заново: /order")
        return
    try:
        prices = [unit_price_usd(item) for item in items]
    except ValueError as exc:
        await message.answer(f"Не получилось рассчитать стоимость: {exc}. Начните /order")
        return
    text = (
        "📋 ПРОВЕРЬТЕ ВАШ ЗАКАЗ — DOX GLOBAL\n\n"
        f"{set_receipt_text(set_code, items, prices)}\n\n"
        f"👤 Получатель: {data.get('full_name', '')}\n"
        f"📱 Телефон: {data.get('phone', '')}\n"
        f"📍 Адрес: {data.get('address', '')}\n"
        f"👥 Код продавца: {data.get('creator_code', '')}\n\n"
        "Проверьте модели, диоптрии и адрес перед подтверждением."
    )
    await state.set_state(dox.OrderForm.confirm)
    await message.answer(dox._clip(text), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить заказ", callback_data="order_ok")],
        [InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="order_redo")],
    ]))


@dox.dp.message(GlobalOrderFlow.street, F.chat.type == "private")
async def global_street(message: Message, state: FSMContext):
    street = (message.text or "").strip()
    if not global_latin_text(street, 5):
        await message.answer("Введите улицу, дом и квартиру латиницей, например Friedrichstrasse 10, Apt 5")
        return
    data = await state.get_data()
    postcode = data.get("postal", "")
    if not postcode:
        postcode = "без индекса (уточнить возможность доставки)"
    combined_address = (
        f"{data['country']}, {data['city']}, {postcode}, {street}"
    )
    await state.update_data(address=combined_address, accessories={})
    # Reuse the existing database, creator notifications, prices, payments and
    # manager status system. All 2/3 selected pairs are present at this point.
    await global_show_review(message, state)


@dox.dp.message(GlobalCommand("cancel"), StateFilter(GlobalOrderFlow), F.chat.type == "private")
async def global_cancel_order(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заполнение отменено. Наберите /order, чтобы начать заново.")


# Lightweight on-server version check. Use /doxversion in @DOXGlobalBot
# to distinguish an installed file from an older deployment.
GLOBAL_UI_VERSION = "DOX Global • AURA 35 USD для 2 пар, SET 1 +5 USD • 23.11.2026 v4"


@dox.dp.message(GlobalCommand("doxversion"), F.chat.type == "private")
async def global_version(message: Message):
    await message.answer(
        f"✅ {GLOBAL_UI_VERSION}\n"
        "После выбора цвета бот отправляет фото, затем отдельное сообщение с кнопками моделей."
    )


class GlobalWelcomeMenuMiddleware(BaseMiddleware):
    """Show the same welcome + set selection after the legacy /start.

    The legacy /start MUST still run, because it stores the deep-link creator
    code (CREATOR01/CREATOR02) in the existing DOX Global database.
    """
    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        if (isinstance(event, Message) and event.chat.type == "private"
                and (event.text or "").split(maxsplit=1)[0].split("@", 1)[0] == "/start"):
            creator = dox._client_creator_code(event.from_user.id)
            if creator:
                await event.answer(
                    "🌍 Добро пожаловать в DOX Global!\n"
                    "🛍 Выберите набор: 2 пары от 66 $ или SET 1 (3 пары) от 110 $.\n"
                    "✨ AURA BLUE / OLIVE / CHOCO / BROWN / GRAY: 35 $ при выборе 2 пар.\n"
                    "🎁 В SET 1 за 110 $ каждая пара AURA добавляет 5 $.\n"
                    "Все подарки и международная доставка бесплатны.\n"
                    "При выборе 2 пар: обычные 33 $, IRIS 35 $, SCLERA 55 $, AURA 35 $.\n"
                    f"Код вашего продавца: {creator}",
                    reply_markup=_set_keyboard(),
                )
        return result


dox.dp.message.middleware(GlobalWelcomeMenuMiddleware())

# Put specific custom handlers ahead of the bot's old generic message
# handlers, so no older text handler accidentally absorbs the wizard input.
_global_flow_callbacks = {
    global_choose_set, global_select_category, global_back_to_category,
    global_select_model, global_postal_skip,
}
_global_flow_messages = {
    global_od, global_os, global_recipient_name, global_recipient_phone,
    global_country, global_city, global_postal, global_street,
    global_cancel_order, global_version,
}
for _name, _callbacks, _handlers in (
    ("callback", _global_flow_callbacks, dox.dp.callback_query.handlers),
    ("message", _global_flow_messages, dox.dp.message.handlers),
):
    _ahead = [h for h in _handlers if h.callback in _callbacks]
    for _handler in _ahead:
        _handlers.remove(_handler)
    _handlers[0:0] = _ahead

if __name__ == "__main__":
    asyncio.run(dox.main())
