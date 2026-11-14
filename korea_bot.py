"""DOX Korea: 100,000 KRW base set + 10,000 KRW per selected AURA pair.

For deployment: install requirements.txt, set BOT_TOKEN, KAKAO_BANK_ACCOUNT,
KAKAO_BANK_HOLDER, run korea_bot.py and bind each creator group via /bind CREATOR01 or /bind CREATOR02.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from korea_catalog import (
    BY_CATEGORY, BY_CODE, CATEGORIES, REQUIRED_PAIRS, SET_PRICE_KRW,
    diopter_hint, normalize_diopter,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = (os.getenv("KOREA_BOT_TOKEN", "").strip() or
         os.getenv("BOT_TOKEN", "").strip())
BANK_ACCOUNT = (os.getenv("KOREA_BANK_ACCOUNT", "").strip() or
                os.getenv("KAKAO_BANK_ACCOUNT", "").strip())
BANK_HOLDER = (os.getenv("KOREA_BANK_HOLDER", "").strip() or
               os.getenv("KAKAO_BANK_HOLDER", "").strip())
DB_PATH = Path(os.getenv("KOREA_DB_PATH", str(BASE_DIR / "data" / "dox_korea.db")))
IMAGE_DIR = BASE_DIR / "korea_images"
# Creator codes in deep links: /start CREATOR01, /start CREATOR02, etc.
# Keep separate from DOX Global creator groups and their database.
CREATOR_CODES = tuple(dict.fromkeys(
    code.strip().upper() for code in
    os.getenv("KOREA_CREATOR_CODES", "CREATOR01,CREATOR02").split(",")
    if re.fullmatch(r"[A-Z0-9_-]{2,32}", code.strip().upper())
))
if not CREATOR_CODES:
    raise RuntimeError("KOREA_CREATOR_CODES must list at least one valid creator code")
OWNER_ID_RAW = os.getenv("KOREA_OWNER_ID", "").strip()
KOREA_OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdecimal() else None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dox-korea")
dp = Dispatcher()

# Exact names only: AURORA GRAY and other similarly named models have no surcharge.
AURA_SURCHARGE_KRW = 10_000
AURA_MODELS = frozenset({
    "AURA BLUE", "AURA OLIVE", "AURA CHOCO", "AURA BROWN", "AURA GRAY",
})


def format_krw(amount: int) -> str:
    return f"{int(amount):,}".replace(",", " ")


def aura_pair_count(pairs: list[dict]) -> int:
    return sum(str(p.get("model", "")).strip().upper() in AURA_MODELS for p in pairs)


def set_total_krw(pairs: list[dict]) -> int:
    return SET_PRICE_KRW + AURA_SURCHARGE_KRW * aura_pair_count(pairs)


def price_lines(pairs: list[dict]) -> str:
    count = aura_pair_count(pairs)
    return (f"Базовая стоимость: {format_krw(SET_PRICE_KRW)} ₩\n"
            f"Доплата за AURA: {count} × {format_krw(AURA_SURCHARGE_KRW)} ₩ "
            f"= {format_krw(count * AURA_SURCHARGE_KRW)} ₩\n"
            f"💰 ИТОГО: {format_krw(set_total_krw(pairs))} ₩")


class OrderFlow(StatesGroup):
    category = State()
    model = State()
    od = State()
    os = State()
    full_name = State()
    phone = State()
    postal = State()
    address = State()
    address_detail = State()
    review = State()
    receipt = State()


def conn():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL,
            manager_chat_id INTEGER NOT NULL,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            postal TEXT NOT NULL,
            address TEXT NOT NULL,
            address_detail TEXT NOT NULL,
            pairs_json TEXT NOT NULL,
            amount_krw INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting_receipt',
            receipt_count INTEGER NOT NULL DEFAULT 0,
            tracking TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            shipped_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS group_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id, message_id),
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS creator_groups (
            creator_code TEXT PRIMARY KEY,
            group_id INTEGER NOT NULL UNIQUE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS buyer_creators (
            buyer_id INTEGER PRIMARY KEY,
            creator_code TEXT NOT NULL
        )""")
        # Migration for existing DOX Korea installations: preserve existing orders.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
        if "creator_code" not in columns:
            db.execute("ALTER TABLE orders ADD COLUMN creator_code TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_orders_creator_code ON orders(creator_code)")


def get_creator_group(code: str) -> int | None:
    with conn() as db:
        row = db.execute("SELECT group_id FROM creator_groups WHERE creator_code=?", (code,)).fetchone()
    return int(row["group_id"]) if row else None


def bind_creator_group(code: str, group_id: int) -> str:
    # Do not allow one group to silently take another creator's code.
    with conn() as db:
        previous = db.execute("SELECT group_id FROM creator_groups WHERE creator_code=?", (code,)).fetchone()
        if previous:
            return "already" if int(previous["group_id"]) == group_id else "taken"
        occupied = db.execute("SELECT creator_code FROM creator_groups WHERE group_id=?", (group_id,)).fetchone()
        if occupied:
            return "group_taken"
        db.execute("INSERT INTO creator_groups(creator_code,group_id) VALUES(?,?)", (code, group_id))
        return "ok"


def remember_buyer_creator(buyer_id: int, code: str):
    with conn() as db:
        db.execute("""INSERT INTO buyer_creators(buyer_id,creator_code) VALUES(?,?)
            ON CONFLICT(buyer_id) DO UPDATE SET creator_code=excluded.creator_code""",
            (buyer_id, code))


def get_buyer_creator(buyer_id: int) -> str | None:
    with conn() as db:
        row = db.execute("SELECT creator_code FROM buyer_creators WHERE buyer_id=?", (buyer_id,)).fetchone()
    return row["creator_code"] if row else None


def create_order(buyer_id: int, creator_code: str, manager_chat_id: int, data: dict) -> int:
    if creator_code not in CREATOR_CODES:
        raise ValueError("Invalid creator code")
    if len(data.get("pairs", [])) != REQUIRED_PAIRS:
        raise ValueError("Set requires exactly three lens pairs")
    with conn() as db:
        cur = db.execute("""INSERT INTO orders
            (buyer_id, creator_code, manager_chat_id, first_name, last_name, phone,
             postal, address, address_detail, pairs_json, amount_krw, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (buyer_id, creator_code, manager_chat_id, data["first_name"], data["last_name"],
             data["phone"], data["postal"], data["address"],
             data["address_detail"], json.dumps(data["pairs"], ensure_ascii=False),
             set_total_krw(data["pairs"]), datetime.now(timezone.utc).isoformat()))
        return int(cur.lastrowid)


def get_order(order_id: int):
    with conn() as db:
        return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def record_group_message(chat_id: int, message_id: int, order_id: int):
    with conn() as db:
        db.execute("INSERT OR IGNORE INTO group_messages(chat_id,message_id,order_id) VALUES(?,?,?)",
                   (chat_id, message_id, order_id))


def order_from_reply(chat_id: int, message_id: int):
    with conn() as db:
        row = db.execute("SELECT order_id FROM group_messages WHERE chat_id=? AND message_id=?",
                         (chat_id, message_id)).fetchone()
    return get_order(int(row["order_id"])) if row else None


def add_receipt(order_id: int) -> bool:
    with conn() as db:
        cur = db.execute("""UPDATE orders SET status='receipt_received', receipt_count=receipt_count+1
            WHERE id=? AND status IN ('waiting_receipt','receipt_received')""", (order_id,))
        return cur.rowcount == 1


def mark_paid(order_id: int) -> str:
    with conn() as db:
        cur = db.execute("""UPDATE orders SET status='paid', paid_at=?
            WHERE id=? AND status='receipt_received' AND receipt_count>0""",
            (datetime.now(timezone.utc).isoformat(), order_id))
        if cur.rowcount:
            return "ok"
        row = db.execute("SELECT status,receipt_count FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return "missing"
        if row["status"] in ("paid", "shipped"):
            return "already"
        if not row["receipt_count"]:
            return "need_receipt"
        return "invalid"


def mark_shipped(order_id: int, tracking: str) -> str:
    with conn() as db:
        cur = db.execute("""UPDATE orders SET status='shipped', tracking=?, shipped_at=?
            WHERE id=? AND status='paid'""",
            (tracking, datetime.now(timezone.utc).isoformat(), order_id))
        if cur.rowcount:
            return "ok"
        row = db.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return "missing"
        if row["status"] == "shipped":
            return "already"
        return "need_paid"


def mark_cancelled(order_id: int) -> bool:
    with conn() as db:
        cur = db.execute("UPDATE orders SET status='cancelled' WHERE id=? "
                         "AND status IN ('waiting_receipt','receipt_received')", (order_id,))
        return bool(cur.rowcount)


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=payload) for text, payload in row]
        for row in rows
    ])


def start_keyboard():
    return inline([[('🛍 Заказать PREMIUM SET 1 — от 100 000 ₩', 'begin')]])


def categories_keyboard(pair_number: int):
    rows = [[(title, f"cat:{pair_number}:{code}")]
            for code, (title, _) in CATEGORIES.items()]
    return inline(rows)


def models_keyboard(pair_number: int, category: str):
    return inline([[(m.name, f"model:{pair_number}:{m.code}")]
                   for m in BY_CATEGORY[category]] + [[("⬅️ Назад к цветам", f"back:{pair_number}")]])


def manager_buttons(order_id: int):
    return inline([
        [("💰 Оплачен", f"paid:{order_id}"), ("📦 Отправлен", f"ship:{order_id}")],
        [("❌ Отменить", f"cancel:{order_id}")],
    ])


def pair_lines(pairs: list[dict], show_aura_charge: bool = False) -> str:
    return "\n".join(
        f"{i}. {p['model']} — OD {p['od']} / OS {p['os']}"
        + (f" (+{format_krw(AURA_SURCHARGE_KRW)} ₩ за AURA)"
           if show_aura_charge and str(p['model']).strip().upper() in AURA_MODELS else "")
        for i, p in enumerate(pairs, 1)
    )


def order_review(data: dict) -> str:
    return (
        "🛍 DOX KOREA — ПРОВЕРЬТЕ ЗАКАЗ\n\n"
        f"Креатор: {data['creator_code']}\n"
        "PREMIUM SET 1\n" + pair_lines(data["pairs"], show_aura_charge=True) + "\n\n"
        "🎁 Сумочка DOX — в комплекте\n"
        "🎁 3 дорожных набора — в комплекте\n"
        "🎁 2 раствора по 150 мл — в комплекте\n"
        "🚚 Доставка по Южной Корее — бесплатно\n\n"
        f"Получатель: {data['first_name']} {data['last_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Индекс: {data.get('postal') or 'Не указан'}\n"
        f"Адрес: {data['address']}\n"
        f"Уточнение адреса: {data['address_detail']}\n\n"
        + price_lines(data["pairs"]) + "\n\n"
        "Проверьте модель, диоптрии и адрес перед подтверждением."
    )


def manager_order_text(order) -> str:
    pairs = json.loads(order["pairs_json"])
    return (
        f"🆕 DOX KOREA — ЗАКАЗ №{order['id']}\n"
        f"Креатор: {order['creator_code'] or 'не указан'}\n"
        f"PREMIUM SET 1 — {format_krw(order['amount_krw'])} ₩\n"
        f"Клиент: {order['first_name']} {order['last_name']}\n"
        f"Телефон: {order['phone']}\n"
        f"Индекс: {order['postal'] or 'Не указан'}\n"
        f"Адрес: {order['address']}\n"
        f"Детали: {order['address_detail']}\n\n"
        "Линзы:\n" + pair_lines(pairs, show_aura_charge=(int(order["amount_krw"]) == set_total_krw(pairs))) + "\n\n"
        "Подарки: сумочка DOX, 3 дорожных набора, 2 раствора 150 мл.\n"
        f"ID клиента: {order['buyer_id']}\n"
        "Статус: ожидает скриншот оплаты.\n"
        "Чтобы написать клиенту, ответьте на это сообщение (Reply)."
    )


def bank_text(order_id: int, amount_krw: int) -> str:
    return (
        f"✅ Заказ №{order_id} подтверждён!\n\n"
        f"💰 К оплате: {format_krw(amount_krw)} ₩\n"
        "🚚 Доставка по Южной Корее — бесплатно.\n\n"
        "🏦 Банковский перевод:\n"
        "Банк: Kakao Bank (카카오뱅크)\n"
        f"Номер счёта: {BANK_ACCOUNT}\n"
        f"Получатель: {BANK_HOLDER}\n\n"
        "После перевода прикрепите фотографию или скриншот оплаты. "
        "Менеджер проверит поступление средств."
    )


def bank_ready() -> bool:
    return bool(BANK_ACCOUNT and BANK_HOLDER)


async def begin_flow(message: Message, state: FSMContext, buyer_id: int):
    code = get_buyer_creator(buyer_id)
    if not code:
        await message.answer("Для заказа откройте персональную ссылку вашего продавца (креатора).")
        return
    if not get_creator_group(code):
        await message.answer("Ваш продавец пока не подключён к боту. Свяжитесь с ним для оформления заказа.")
        return
    if not bank_ready():
        await message.answer("Оплата пока настраивается. Приём заказов временно закрыт.")
        return
    await state.clear()
    await state.update_data(pairs=[], creator_code=code)
    await next_pair(message, state)


async def next_pair(message: Message, state: FSMContext):
    data = await state.get_data()
    count = len(data.get("pairs", []))
    if count >= REQUIRED_PAIRS:
        await state.set_state(OrderFlow.full_name)
        await message.answer(
            "✅ Вы выбрали все 3 пары.\n\n"
            "Введите ИМЯ И ФАМИЛИЮ одним сообщением, английскими буквами.\n"
            "Например: ANNA IVANOVA"
        )
        return
    number = count + 1
    await state.update_data(pair_number=number)
    await state.set_state(OrderFlow.category)
    await message.answer(f"👁 Пара {number} из 3. Выберите цветовой каталог:",
                         reply_markup=categories_keyboard(number))


@dp.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, state: FSMContext):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        code = parts[1].strip().upper()
        if code not in CREATOR_CODES:
            await message.answer("Ссылка креатора неверная. Попросите у продавца правильную ссылку.")
            return
        if get_creator_group(code) is None:
            await message.answer("Этот креатор ещё не подключён. Попросите продавца активировать свою ссылку.")
            return
        # The latest intentional creator-link visit determines future orders;
        # creator_code on a completed order is immutable.
        remember_buyer_creator(message.from_user.id, code)
    code = get_buyer_creator(message.from_user.id)
    if not code:
        await message.answer("Добро пожаловать в DOX Korea! 🇰🇷\n\n"
                             "Чтобы оформить PREMIUM SET 1, откройте ссылку от вашего продавца.")
        return
    caption = (
        "🇰🇷 Добро пожаловать в DOX Korea!\n\n"
        "🎁 PREMIUM SET 1 — от 100 000 ₩\n"
        "✨ Серия AURA (BLUE, OLIVE, CHOCO, BROWN, GRAY): "
        "+10 000 ₩ за каждую выбранную пару.\n"
        "Цена за 3 пары: от 100 000 до 130 000 ₩.\n\n"
        "• 3 пары линз на ваш выбор\n"
        "• Фирменная сумочка DOX\n"
        "• 3 дорожных набора (контейнер и щипчики)\n"
        "• 2 корейских раствора по 150 мл\n"
        "🚚 Бесплатная доставка по Южной Корее.\n\n"
        "Общение и оформление — на русском языке.\n"
        f"Код вашего продавца: {code}"
    )
    photo = IMAGE_DIR / "premium_set.jpg"
    if photo.exists():
        await message.answer_photo(FSInputFile(photo), caption=caption, reply_markup=start_keyboard())
    else:
        await message.answer(caption, reply_markup=start_keyboard())


@dp.message(Command("order"), F.chat.type == "private")
async def order_command(message: Message, state: FSMContext):
    await begin_flow(message, state, message.from_user.id)


@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_draft(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заполнение отменено. Отправьте /start, чтобы начать заново.\n"
                         "Уже подтверждённые заказы эта команда не отменяет.")


@dp.callback_query(F.data == "begin")
async def begin_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.message.chat.type != "private":
        return
    await begin_flow(call.message, state, call.from_user.id)


@dp.callback_query(F.data.startswith("cat:"))
async def category_callback(call: CallbackQuery, state: FSMContext):
    try:
        _, number, code = call.data.split(":")
        number = int(number)
    except (ValueError, AttributeError):
        await call.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    if (await state.get_state() != OrderFlow.category.state or
            data.get("pair_number") != number or code not in CATEGORIES):
        await call.answer("Кнопка устарела. Используйте последний шаг заказа.", show_alert=True)
        return
    await state.update_data(category=code)
    await state.set_state(OrderFlow.model)
    title, filename = CATEGORIES[code]
    photo = IMAGE_DIR / filename
    await call.answer()
    text = f"👁 Пара {number} из 3. Каталог: {title}\nВыберите модель под фотографией."
    if photo.is_file():
        await call.message.answer_photo(FSInputFile(photo), caption=text,
                                        reply_markup=models_keyboard(number, code))
    else:
        await call.message.answer(text, reply_markup=models_keyboard(number, code))


@dp.callback_query(F.data.startswith("back:"))
async def back_callback(call: CallbackQuery, state: FSMContext):
    try:
        number = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        await call.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    if (await state.get_state() != OrderFlow.model.state or
            data.get("pair_number") != number):
        await call.answer("Кнопка устарела.", show_alert=True)
        return
    await state.set_state(OrderFlow.category)
    await call.answer()
    await call.message.answer(f"Выберите каталог для пары {number}:",
                              reply_markup=categories_keyboard(number))


@dp.callback_query(F.data.startswith("model:"))
async def model_callback(call: CallbackQuery, state: FSMContext):
    try:
        _, n, code = call.data.split(":")
        n = int(n)
    except (ValueError, AttributeError):
        await call.answer("Неверная кнопка", show_alert=True)
        return
    data = await state.get_data()
    model = BY_CODE.get(code)
    if (await state.get_state() != OrderFlow.model.state or
            data.get("pair_number") != n or not model or data.get("category") != model.category):
        await call.answer("Выберите модель на последнем шаге заказа.", show_alert=True)
        return
    await call.answer()
    await state.update_data(model_code=code)
    if len(model.diopters) == 1:
        pairs = data["pairs"] + [{"model": model.name, "od": "0.00", "os": "0.00"}]
        await state.update_data(pairs=pairs, model_code=None)
        extra = (f" Доплата за AURA: +{format_krw(AURA_SURCHARGE_KRW)} ₩."
                 if model.name in AURA_MODELS else "")
        await call.message.answer(f"✅ Пара {n}: {model.name}. Диоптрии 0.00 / 0.00 (только нулевые).{extra}"
                                  f"\nТекущая цена сета: {format_krw(set_total_krw(pairs))} ₩.")
        await next_pair(call.message, state)
        return
    await state.set_state(OrderFlow.od)
    await call.message.answer(
        f"👁 Пара {n}: {model.name}\n"
        + (f"✨ Доплата за AURA: +{format_krw(AURA_SURCHARGE_KRW)} ₩.\n"
           if model.name in AURA_MODELS else "")
        + "\nВведите диоптрию ПРАВОГО глаза (OD).\n"
        f"{diopter_hint(model)}\n"
        "Для линз без диоптрий введите 0.00."
    )


@dp.message(OrderFlow.od, F.chat.type == "private")
async def right_power(message: Message, state: FSMContext):
    data = await state.get_data()
    model = BY_CODE[data["model_code"]]
    value = normalize_diopter(message.text or "", model)
    if value is None:
        await message.answer(f"Эта диоптрия не указана для {model.name}.\n{diopter_hint(model)}")
        return
    await state.update_data(od=value)
    await state.set_state(OrderFlow.os)
    await message.answer(f"Теперь введите диоптрию ЛЕВОГО глаза (OS) для {model.name}.\n"
                         f"{diopter_hint(model)}")


@dp.message(OrderFlow.os, F.chat.type == "private")
async def left_power(message: Message, state: FSMContext):
    data = await state.get_data()
    model = BY_CODE[data["model_code"]]
    value = normalize_diopter(message.text or "", model)
    if value is None:
        await message.answer(f"Эта диоптрия не указана для {model.name}.\n{diopter_hint(model)}")
        return
    pairs = data["pairs"] + [{"model": model.name, "od": data["od"], "os": value}]
    await state.update_data(pairs=pairs, model_code=None, od=None)
    extra = (f" Доплата за AURA: +{format_krw(AURA_SURCHARGE_KRW)} ₩."
             if model.name in AURA_MODELS else "")
    await message.answer(f"✅ Сохранена пара {len(pairs)}: {model.name}, OD {data['od']} / OS {value}.{extra}"
                         f"\nТекущая цена сета: {format_krw(set_total_krw(pairs))} ₩.")
    await next_pair(message, state)


def valid_latin_name(value: str) -> bool:
    value = value.strip()
    return bool(re.fullmatch(r"[A-Za-z]+(?:[ '\-][A-Za-z]+)*", value)) and len(value) <= 60


def split_latin_full_name(value: str) -> tuple[str, str] | None:
    """One customer input; preserve existing first_name/last_name DB fields."""
    cleaned = " ".join(value.strip().split())
    if not valid_latin_name(cleaned):
        return None
    parts = cleaned.split(" ", maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    return parts[0], parts[1]


@dp.message(OrderFlow.full_name, F.chat.type == "private")
async def full_name(message: Message, state: FSMContext):
    parsed = split_latin_full_name(message.text or "")
    if parsed is None:
        await message.answer(
            "Введите имя и фамилию ОДНИМ сообщением английскими буквами.\n"
            "Например: ANNA IVANOVA"
        )
        return
    first, last = parsed
    await state.update_data(first_name=first, last_name=last)
    await state.set_state(OrderFlow.phone)
    await message.answer("Введите номер мобильного телефона в Корее, например 010-1234-5678.")


def valid_korea_phone(raw: str) -> bool:
    stripped = re.sub(r"[\s()\-]", "", raw)
    return bool(re.fullmatch(r"010\d{8}|\+8210\d{8}|8210\d{8}", stripped))


@dp.message(OrderFlow.phone, F.chat.type == "private")
async def phone(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not valid_korea_phone(value):
        await message.answer("Укажите корейский номер в формате 010-1234-5678 или +82 10 1234 5678.")
        return
    await state.update_data(phone=value)
    await state.set_state(OrderFlow.postal)
    await message.answer(
        "Почтовый индекс (우편번호) — НЕОБЯЗАТЕЛЬНО.\n"
        "Если знаете, введите 5 цифр, например 12345.\n"
        "Если нет — нажмите «Пропустить индекс» или отправьте «-».",
        reply_markup=inline([[('⏭ Пропустить индекс', 'skip_postal')]])
    )


async def proceed_to_address(message: Message, state: FSMContext, postal_value: str):
    await state.update_data(postal=postal_value)
    await state.set_state(OrderFlow.address)
    await message.answer(
        "Введите ПОЛНЫЙ адрес доставки на корейском языке (한글).\n"
        "Например: 서울특별시 강남구 테헤란로 123"
    )


@dp.callback_query(F.data == "skip_postal")
async def skip_postal(call: CallbackQuery, state: FSMContext):
    if call.message.chat.type != "private" or await state.get_state() != OrderFlow.postal.state:
        await call.answer("Кнопка больше неактуальна.", show_alert=True)
        return
    await call.answer()
    await proceed_to_address(call.message, state, "")


@dp.message(OrderFlow.postal, F.chat.type == "private")
async def postal(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if value.lower() in ("-", "пропустить", "пропустить индекс", "skip"):
        await proceed_to_address(message, state, "")
        return
    if not re.fullmatch(r"[0-9]{5}", value):
        await message.answer(
            "Если указываете почтовый индекс, нужны ровно 5 цифр.\n"
            "Или нажмите «Пропустить индекс».",
            reply_markup=inline([[('⏭ Пропустить индекс', 'skip_postal')]])
        )
        return
    await proceed_to_address(message, state, value)


@dp.message(OrderFlow.address, F.chat.type == "private")
async def address(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not (8 <= len(value) <= 250 and re.search(r"[가-힣]", value)):
        await message.answer("Адрес должен содержать корейские буквы и быть полным. "
                             "Например: 서울특별시 강남구 테헤란로 123")
        return
    await state.update_data(address=value)
    await state.set_state(OrderFlow.address_detail)
    await message.answer("Укажите номер квартиры, корпуса, этажа или офиса на корейском языке.\n"
                         "Например: 101동 502호. Если уточнений нет, отправьте «-».")


@dp.message(OrderFlow.address_detail, F.chat.type == "private")
async def address_detail(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value or len(value) > 120 or (value != "-" and not re.search(r"[가-힣]", value)):
        await message.answer("Напишите детальный адрес на корейском (например, 101동 502호) или «-».")
        return
    await state.update_data(address_detail=value)
    await state.set_state(OrderFlow.review)
    data = await state.get_data()
    await message.answer(order_review(data), reply_markup=inline([
        [("✅ Подтвердить заказ", "confirm")],
        [("🔁 Заполнить заново", "restart")],
    ]))


@dp.callback_query(F.data == "restart")
async def restart_flow(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if call.message.chat.type == "private":
        await begin_flow(call.message, state, call.from_user.id)


@dp.callback_query(F.data == "confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    if await state.get_state() != OrderFlow.review.state:
        await call.answer("Этот заказ уже подтверждён или форма устарела.", show_alert=True)
        return
    data = await state.get_data()
    if len(data.get("pairs", [])) != REQUIRED_PAIRS:
        await call.answer("Нужно выбрать ровно 3 пары линз.", show_alert=True)
        return
    code = data.get("creator_code")
    group = get_creator_group(code) if code in CREATOR_CODES else None
    if group is None or not bank_ready():
        await call.answer("Менеджер или оплата пока недоступны.", show_alert=True)
        return
    await call.answer()
    await state.clear()  # prevents accepting a second confirmation from stale buttons
    order_id = create_order(call.from_user.id, code, group, data)
    row = get_order(order_id)
    try:
        sent = await bot.send_message(group, manager_order_text(row),
                                      reply_markup=manager_buttons(order_id))
    except TelegramAPIError:
        log.exception("Cannot deliver new order #%s to manager group", order_id)
        await state.clear()
        await call.message.answer("Заказ записан, но передать его менеджеру не удалось. "
                                  "Оплату пока НЕ переводите. Обратитесь к администратору.")
        return
    record_group_message(group, sent.message_id, order_id)
    await state.clear()
    await call.message.answer(bank_text(order_id, row["amount_krw"]), reply_markup=inline([
        [("📷 Прикрепить скриншот оплаты", f"receipt:{order_id}")]
    ]))


@dp.callback_query(F.data.startswith("receipt:"))
async def receipt_begin(call: CallbackQuery, state: FSMContext):
    try:
        order_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Неверный номер заказа", show_alert=True)
        return
    order = get_order(order_id)
    if not order or order["buyer_id"] != call.from_user.id:
        await call.answer("Это не ваш заказ.", show_alert=True)
        return
    if order["status"] in ("paid", "shipped", "cancelled"):
        await call.answer("Приём чека по этому заказу закрыт.", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.set_state(OrderFlow.receipt)
    await state.update_data(receipt_order_id=order_id)
    await call.message.answer(
        f"📷 Заказ №{order_id}\nПришлите фото или скриншот перевода как изображение. "
        "Можно отправить JPG/PNG файлом.\n"
        "Не отправляйте пароли, полный номер банковской карты или другие секретные данные."
    )


@dp.message(OrderFlow.receipt, F.chat.type == "private")
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data.get("receipt_order_id")
    order = get_order(order_id) if order_id else None
    if not order or order["buyer_id"] != message.from_user.id or order["status"] not in ("waiting_receipt", "receipt_received"):
        await state.clear()
        await message.answer("Заказ не найден или уже обработан. Отправьте /start для нового заказа.")
        return
    photo_id = message.photo[-1].file_id if message.photo else None
    document_id = None
    if message.document and message.document.mime_type in ("image/jpeg", "image/png", "image/webp"):
        document_id = message.document.file_id
    if not photo_id and not document_id:
        await message.answer("Пришлите именно ФОТО чека или JPG/PNG-файл с изображением оплаты.")
        return
    caption = (f"📷 ЧЕК ПО ЗАКАЗУ №{order_id}\n"
               f"Креатор: {order['creator_code'] or 'не указан'}\n"
               f"Клиент: {order['first_name']} {order['last_name']}\n"
               f"Проверьте поступление {format_krw(order['amount_krw'])} ₩ на Kakao Bank.\n"
               "После проверки нажмите «Оплачен» в заказе.\n"
               "Ответить клиенту можно ответом (Reply) на это сообщение.")
    try:
        if photo_id:
            delivered = await bot.send_photo(order["manager_chat_id"], photo_id, caption=caption,
                                              reply_markup=manager_buttons(order_id))
        else:
            delivered = await bot.send_document(order["manager_chat_id"], document_id, caption=caption,
                                                 reply_markup=manager_buttons(order_id))
    except TelegramAPIError:
        log.exception("Cannot deliver receipt for order #%s", order_id)
        await message.answer("Не удалось передать чек менеджеру. Отправьте изображение повторно.")
        return
    record_group_message(order["manager_chat_id"], delivered.message_id, order_id)
    if not add_receipt(order_id):
        await state.clear()
        await message.answer("Чек передан, но статус заказа изменился. Свяжитесь с менеджером.")
        return
    await state.clear()
    await message.answer(f"✅ Скриншот оплаты заказа №{order_id} отправлен менеджеру.\n"
                         "После проверки поступления денег вы получите уведомление «Оплата подтверждена».")


async def group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramAPIError:
        return False


@dp.message(Command("myid"), F.chat.type == "private")
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}\n"
                         "Для защиты привязки продавцов владелец бота должен "
                         "сохранить этот номер в приватном .env: KOREA_OWNER_ID=...")


@dp.message(Command("bind"), F.chat.type.in_({"group", "supergroup"}))
async def bind_group(message: Message, bot: Bot):
    if not KOREA_OWNER_ID:
        await message.answer("⚠️ Привязка групп отключена для безопасности. "
                             "Владелец должен указать KOREA_OWNER_ID в приватном .env на сервере.")
        return
    if (not message.from_user or message.from_user.id != KOREA_OWNER_ID or
            not await group_admin(bot, message.chat.id, message.from_user.id)):
        await message.answer("Привязать креатора может только владелец бота, "
                             "который является администратором этой группы.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].strip().upper() not in CREATOR_CODES:
        await message.answer("Используйте команду /bind CREATOR01 или /bind CREATOR02.")
        return
    code = parts[1].strip().upper()
    result = bind_creator_group(code, message.chat.id)
    if result == "taken":
        await message.answer("⚠️ Этот код уже привязан к другой группе. "
                             "Привязка не изменена — обратитесь к владельцу бота.")
        return
    if result == "group_taken":
        await message.answer("⚠️ Эта группа уже привязана к другому креатору. "
                             "Для нового креатора создайте отдельную группу.")
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    await message.answer(
        f"✅ Группа привязана к коду {code}!\n\n"
        f"Персональная ссылка продавца:\n{link}\n\n"
        "Заказы и чеки по этой ссылке будут приходить ТОЛЬКО в эту группу.\n"
        "Администратор группы может подтверждать оплату, отправку и отвечать клиентам."
    )


@dp.message(Command("mycreator"), F.chat.type == "private")
async def my_creator(message: Message, bot: Bot):
    code = get_buyer_creator(message.from_user.id)
    if not code:
        await message.answer("Вы пока не привязаны к креатору. Откройте ссылку своего продавца.")
        return
    await message.answer(f"Ваш креатор: {code}")


async def authorized_manager(call: CallbackQuery, bot: Bot, order) -> bool:
    return bool(order and call.message and
                call.message.chat.id == order["manager_chat_id"] and
                call.from_user and
                await group_admin(bot, call.message.chat.id, call.from_user.id))


@dp.callback_query(F.data.startswith("paid:"))
async def paid_callback(call: CallbackQuery, bot: Bot):
    try:
        order_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный номер", show_alert=True)
        return
    order = get_order(order_id)
    if not await authorized_manager(call, bot, order):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    result = mark_paid(order_id)
    if result == "need_receipt":
        await call.answer("Сначала клиент должен прислать скриншот оплаты.", show_alert=True)
    elif result == "already":
        await call.answer("Оплата уже подтверждена.", show_alert=True)
    elif result != "ok":
        await call.answer("Изменить статус не удалось.", show_alert=True)
    else:
        await call.answer("Оплата подтверждена")
        await call.message.answer(f"✅ Заказ №{order_id}: оплата подтверждена менеджером.")
        try:
            await bot.send_message(order["buyer_id"],
                f"✅ DOX Korea — оплата подтверждена!\n\n"
                f"Оплата заказа №{order_id} на сумму {format_krw(order['amount_krw'])} ₩ проверена менеджером.\n"
                "Ваш PREMIUM SET 1 готовится к отправке. Спасибо за покупку!")
        except TelegramAPIError:
            log.exception("Cannot notify buyer about paid order #%s", order_id)


@dp.callback_query(F.data.startswith("ship:"))
async def shipping_callback(call: CallbackQuery, bot: Bot):
    try:
        order_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный номер", show_alert=True)
        return
    order = get_order(order_id)
    if not await authorized_manager(call, bot, order):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    if order["status"] == "shipped":
        await call.answer("Уже отправлен.", show_alert=True)
        return
    if order["status"] != "paid":
        await call.answer("Сначала подтвердите оплату.", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"🚚 ТРЕК-НОМЕР ДЛЯ ЗАКАЗА №{order_id}\n"
        "ОТВЕТЬТЕ на это сообщение (Reply) и напишите службу доставки и трек-номер.\n"
        "Например: CJ대한통운 123456789012",
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(call: CallbackQuery, bot: Bot):
    try:
        order_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный номер", show_alert=True)
        return
    order = get_order(order_id)
    if not await authorized_manager(call, bot, order):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    if not mark_cancelled(order_id):
        await call.answer("Нельзя отменить этот заказ через кнопку.", show_alert=True)
        return
    await call.answer("Заказ отменён")
    await call.message.answer(f"❌ Заказ №{order_id} отменён менеджером.")
    try:
        await bot.send_message(order["buyer_id"],
                               f"❌ DOX Korea: заказ №{order_id} отменён. "
                               "Если вы уже перевели деньги, свяжитесь с менеджером.")
    except TelegramAPIError:
        log.exception("Cannot notify buyer about cancelled order #%s", order_id)


@dp.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
async def manager_reply(message: Message, bot: Bot):
    if not message.from_user or not await group_admin(bot, message.chat.id, message.from_user.id):
        return
    quoted = message.reply_to_message
    if quoted and quoted.from_user and quoted.from_user.id == bot.id and quoted.text:
        match = re.match(r"🚚 ТРЕК-НОМЕР ДЛЯ ЗАКАЗА №(\d+)", quoted.text)
        if match:
            order_id = int(match.group(1))
            order = get_order(order_id)
            if not order or order["manager_chat_id"] != message.chat.id:
                await message.answer("Заказ не найден в этой группе.")
                return
            tracking = (message.text or "").strip()
            if len(tracking) < 4 or len(tracking) > 120 or "\n" in tracking:
                await message.answer("Укажите службу доставки и трек-номер одной строкой (4–120 символов).")
                return
            status = mark_shipped(order_id, tracking)
            if status == "ok":
                await message.answer(f"📦 Заказ №{order_id} отмечен как отправленный.\nТрек: {tracking}")
                try:
                    await bot.send_message(order["buyer_id"],
                                           f"📦 DOX Korea — ваш заказ №{order_id} отправлен!\n\n"
                                           f"Номер отслеживания: {tracking}\n"
                                           "Спасибо, что выбрали DOX Korea!")
                except TelegramAPIError:
                    log.exception("Cannot notify buyer about shipped order #%s", order_id)
            elif status == "already":
                await message.answer("Этот заказ уже отмечен как отправленный.")
            else:
                await message.answer("Сначала подтвердите оплату заказа.")
            return
    order = order_from_reply(message.chat.id, quoted.message_id)
    if not order:
        return
    if order["status"] == "cancelled":
        await message.answer("Заказ отменён. Ответ клиенту из этого заказа отключён.")
        return
    try:
        await bot.send_message(order["buyer_id"], f"✉️ Сообщение менеджера DOX Korea по заказу №{order['id']}:")
        await message.copy_to(order["buyer_id"])
        await message.reply("✅ Ответ отправлен клиенту.")
    except TelegramAPIError:
        log.exception("Cannot relay manager response for order #%s", order["id"])
        await message.reply("Не удалось доставить ответ клиенту. Возможно, он заблокировал бота.")


@dp.message(F.chat.type == "private")
async def catch_private(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await message.answer("Следуйте текущему шагу. Чтобы начать заново — /start. "
                             "Чтобы отменить заполнение — /cancel.")
    else:
        await message.answer("Чтобы заказать PREMIUM SET 1 от 100 000 ₩, нажмите /start.")


async def main():
    if not TOKEN or ":" not in TOKEN:
        raise RuntimeError("Укажите KOREA_BOT_TOKEN (или BOT_TOKEN при отдельном запуске) в приватных настройках")
    init_db()
    if not bank_ready():
        log.warning("KAKAO_BANK_ACCOUNT / KAKAO_BANK_HOLDER missing: checkout disabled")
    bot = Bot(TOKEN, default=DefaultBotProperties())
    try:
        me = await bot.get_me()
        log.info("DOX Korea bot started: @%s", me.username)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
