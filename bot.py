import asyncio
import logging
import os
import sqlite3
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

# Токен бота берётся из переменной окружения (её задаём в Railway, не в коде!)
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "bot.db")


def _safe_err(e) -> str:
    """Текст ошибки без токена бота (aiogram иногда вставляет его в сообщение)."""
    return str(e).replace(TOKEN, "<token>")

bot = Bot(TOKEN)
# ВАЖНО: анкета хранится в памяти. Если бот перезапустится посреди анкеты,
# клиенту придётся начать заново командой /order — это нормально для старта.
dp = Dispatcher(storage=MemoryStorage())


class OrderForm(StatesGroup):
    name = State()
    address = State()
    phone = State()
    left_eye = State()
    right_eye = State()
    color = State()
    model = State()
    qty = State()
    more = State()
    confirm = State()

# ---------- База данных (простой файл SQLite) ----------
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS creators (
    code TEXT PRIMARY KEY,      -- код креатора, например ANNA01
    group_id INTEGER            -- id группы, куда идут сообщения его клиентов
);
CREATE TABLE IF NOT EXISTS clients (
    user_id INTEGER PRIMARY KEY,   -- Telegram id клиента
    creator_code TEXT,             -- от какого креатора пришёл
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS msg_map (
    group_id INTEGER,
    message_id INTEGER,            -- сообщение в группе...
    client_id INTEGER,             -- ...относится к этому клиенту
    PRIMARY KEY (group_id, message_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    creator_code TEXT,
    full_name TEXT,
    address TEXT,
    phone TEXT,
    left_eye TEXT,
    right_eye TEXT,
    color TEXT,
    model TEXT,
    qty TEXT,
    status TEXT DEFAULT 'new',       -- new / paid / shipped
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    left_eye TEXT,
    right_eye TEXT,
    color TEXT,
    model TEXT,
    qty TEXT
);
"""
)
db.commit()

# Миграция: добавляем новые столбцы в уже существующую базу, если их ещё нет.
try:
    db.execute("ALTER TABLE orders ADD COLUMN model TEXT")
    db.commit()
except sqlite3.OperationalError:
    pass  # столбец уже есть

GROUP_TYPES = {"group", "supergroup"}


# ---------- Команда /bind КОД (пишется в группе креатора) ----------
@dp.message(Command("bind"), F.chat.type.in_(GROUP_TYPES))
async def bind(message: Message, command: CommandObject):
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ("creator", "administrator"):
        return  # привязывать группу могут только админы группы

    code = (command.args or "").strip().upper()
    if not code:
        await message.reply("Напишите так: /bind ANNA01")
        return

    db.execute(
        "INSERT INTO creators(code, group_id) VALUES(?, ?) "
        "ON CONFLICT(code) DO UPDATE SET group_id = excluded.group_id",
        (code, message.chat.id),
    )
    db.commit()

    me = await bot.get_me()
    await message.reply(
        f"Готово! Группа привязана к коду {code}.\n"
        f"Ссылка для клиентов: https://t.me/{me.username}?start={code}\n\n"
        "Чтобы ответить клиенту, отвечайте реплаем на его сообщение."
    )


# ---------- Команда /orders (список последних заказов в группе) ----------
@dp.message(Command("orders"), F.chat.type.in_(GROUP_TYPES))
async def list_orders(message: Message):
    creator = db.execute(
        "SELECT code FROM creators WHERE group_id = ?", (message.chat.id,)
    ).fetchone()
    if not creator:
        await message.reply("Эта группа ещё не привязана. Сначала выполните /bind КОД.")
        return

    rows = db.execute(
        "SELECT o.id, o.full_name, "
        "(SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id), o.status "
        "FROM orders o WHERE o.creator_code = ? ORDER BY o.id DESC LIMIT 10",
        (creator[0],),
    ).fetchall()
    if not rows:
        await message.reply("Заказов пока нет.")
        return

    lines = ["Последние заказы:"]
    for order_id, full_name, n_items, status in rows:
        lines.append(
            f"№{order_id} · {STATUS_LABELS.get(status, status)} · {full_name} · "
            f"товаров: {n_items or 1}"
        )
    await message.reply("\n".join(lines))


# ---------- Ответ сотрудника/креатора в группе -> клиенту ----------
@dp.message(F.chat.type.in_(GROUP_TYPES), F.reply_to_message)
async def from_staff(message: Message):
    row = db.execute(
        "SELECT client_id FROM msg_map WHERE group_id = ? AND message_id = ?",
        (message.chat.id, message.reply_to_message.message_id),
    ).fetchone()
    if not row:
        return  # это реплай не на сообщение клиента — игнорируем
    try:
        await bot.copy_message(row[0], message.chat.id, message.message_id)
    except TelegramForbiddenError:
        await message.reply("Не удалось доставить: клиент заблокировал бота.")
    except TelegramAPIError as e:
        await message.reply(f"Не удалось доставить: {e}")


# ---------- Клиент нажимает /start (с кодом креатора или без) ----------
@dp.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, command: CommandObject):
    code = (command.args or "").strip().upper()
    user_id = message.from_user.id

    if code:
        exists = db.execute(
            "SELECT 1 FROM creators WHERE code = ?", (code,)
        ).fetchone()
        if not exists:
            await message.answer(
                "Эта ссылка недействительна. Попросите у вашего креатора актуальную."
            )
            return
        # Пока правило простое: последняя открытая ссылка становится основной.
        # Правило закрепления клиента за креатором для комиссии решим позже.
        db.execute(
            "INSERT INTO clients(user_id, creator_code) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET creator_code = excluded.creator_code",
            (user_id, code),
        )
        db.commit()
        await message.answer(
            "Здравствуйте! Вы на связи с командой DOX.\n"
            "Напишите ваше сообщение, и мы ответим прямо здесь.\n\n"
            "Чтобы оформить заказ, напишите /order."
        )
        return

    known = db.execute(
        "SELECT 1 FROM clients WHERE user_id = ?", (user_id,)
    ).fetchone()
    if known:
        await message.answer(
            "Вы уже на связи, просто напишите сообщение, либо /order для заказа."
        )
    else:
        await message.answer(
            "Чтобы связаться с нами, перейдите по персональной ссылке вашего креатора."
        )


# ---------- Анкета заказа (только в личном чате с ботом) ----------
STEP_ORDER = [
    OrderForm.name,
    OrderForm.address,
    OrderForm.phone,
    OrderForm.left_eye,
    OrderForm.right_eye,
    OrderForm.color,
    OrderForm.model,
]

STEP_FIELDS = {
    OrderForm.name: "full_name",
    OrderForm.address: "address",
    OrderForm.phone: "phone",
    OrderForm.left_eye: "left_eye",
    OrderForm.right_eye: "right_eye",
    OrderForm.color: "color",
    OrderForm.model: "model",
}

# Поля одного товара в заказе и ограничение на количество товаров.
ITEM_FIELDS = ["left_eye", "right_eye", "color", "model"]
MAX_ITEMS = 3
FIRST_ITEM_STEP = STEP_ORDER.index(OrderForm.left_eye)


def _format_items(items):
    """Текст со списком товаров заказа (для сводки и карточки в группе)."""
    blocks = []
    for i, it in enumerate(items, 1):
        lines = []
        if len(items) > 1:
            lines.append(f"Товар {i}:")
        lines.append(f"Диоптрии: {it.get('left_eye') or ''} / {it.get('right_eye') or ''}")
        lines.append(f"Цвет: {it.get('color') or ''}")
        lines.append(f"Модель: {it.get('model') or ''}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


STEP_PROMPTS = {
    OrderForm.name: "Имя и фамилия — на английском языке (например: Ivan Petrov).",
    OrderForm.address: (
        "Адрес доставки — на английском языке.\n"
        "Укажите: страна, город, улица, дом, квартира, индекс, e-mail."
    ),
    OrderForm.phone: "Номер телефона для доставки?",
    OrderForm.left_eye: "Диоптрии, левый глаз?",
    OrderForm.right_eye: "Диоптрии, правый глаз?",
    OrderForm.color: "Выберите цвет линз:",
    OrderForm.model: (
        "Напишите название модели линз из каталога выше "
        "(например: MIRA BLUE).\n"
        "Обратите внимание на доступные диоптрии рядом с моделью."
    ),
}

# Список цветов для кнопок на шаге выбора цвета.
COLORS = [
    "Gray",
    "Brown and Choco",
    "Blue",
    "Green",
    "Violet",
    "Black",
    "Прозрачные и специальные линзы",
]

# Папка с картинками каталога (лежит рядом с bot.py в репозитории).
CATALOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")

# Запасной источник картинок: прямые ссылки на файлы в репозитории GitHub.
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/olivetreekorea2026-sudo/dox-bat/main/"

COLOR_CATALOG_FILES = {
    "Gray": "Gray.jpg",
    "Brown and Choco": "Brown_and_Choco.jpg",
    "Blue": "Blue.png",
    "Green": "Green.png",
    "Violet": "Violet.png",
    "Black": "Black.png",
    "Прозрачные и специальные линзы": "Прозрачные_и_специальные_линзы.png",
}


def _back_keyboard(step: int):
    if step <= 0:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="order_back")]]
    )


def _step_keyboard(step: int):
    """Клавиатура для конкретного шага анкеты: кнопки цвета на шаге цвета,
    иначе — просто кнопка «Назад» (или ничего на первом шаге)."""
    if STEP_ORDER[step] == OrderForm.color:
        rows = []
        row = []
        for color in COLORS:
            row.append(InlineKeyboardButton(text=color, callback_data=f"color:{color}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if step > 0:
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="order_back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return _back_keyboard(step)


def _client_creator_code(user_id: int):
    row = db.execute(
        "SELECT creator_code FROM clients WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else None


@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_order(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Сейчас нет активной анкеты.")
        return
    await state.clear()
    await message.answer("Оформление заказа отменено. Напишите /order, чтобы начать заново.")


async def _notify_operator(user, order_id: int) -> bool:
    """Сообщает в группу креатора, что клиент просит связаться. True — если получилось."""
    code = _client_creator_code(user.id)
    if not code:
        return False
    creator = db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (code,)
    ).fetchone()
    if not creator or not creator[0]:
        return False

    name = user.full_name + (f" (@{user.username})" if user.username else "")
    text = f"🔔 Клиент {name} просит связаться с оператором"
    if order_id:
        text += f" по заказу №{order_id}"
    text += f"\nID: {user.id} · код: {code}\nОтветьте реплаем на это сообщение."
    try:
        sent = await bot.send_message(creator[0], text)
    except TelegramAPIError as e:
        logging.error("Не удалось уведомить оператора: %s", _safe_err(e))
        return False
    db.execute(
        "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) VALUES(?, ?, ?)",
        (creator[0], sent.message_id, user.id),
    )
    db.commit()
    return True


OPERATOR_OK_TEXT = (
    "Оператор уведомлён. Напишите ваш вопрос здесь одним или несколькими "
    "сообщениями — мы ответим прямо в этом чате."
)
OPERATOR_FAIL_TEXT = (
    "Не удалось связаться с оператором. Перейдите по персональной ссылке "
    "вашего креатора или попробуйте позже."
)


@dp.message(Command("operator"), F.chat.type == "private")
async def operator_command(message: Message):
    ok = await _notify_operator(message.from_user, 0)
    await message.answer(OPERATOR_OK_TEXT if ok else OPERATOR_FAIL_TEXT)


@dp.callback_query(F.data.startswith("operator:"))
async def operator_button(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split(":", 1)[1] or 0)
    except ValueError:
        order_id = 0
    ok = await _notify_operator(callback.from_user, order_id)
    await callback.message.answer(OPERATOR_OK_TEXT if ok else OPERATOR_FAIL_TEXT)
    await callback.answer()


@dp.message(Command("order"), F.chat.type == "private")
async def start_order(message: Message, state: FSMContext):
    if not _client_creator_code(message.from_user.id):
        await message.answer(
            "Чтобы оформить заказ, перейдите по персональной ссылке вашего креатора."
        )
        return
    await state.set_data({"step": 0, "items": []})
    await state.set_state(STEP_ORDER[0])
    await message.answer(
        "Оформляем заказ. В любой момент можно написать /cancel, чтобы прервать.\n\n"
        + STEP_PROMPTS[STEP_ORDER[0]]
    )


# ---------- Один общий шаг анкеты (вместо отдельного хендлера на каждый вопрос) ----------
@dp.message(StateFilter(*STEP_ORDER), F.chat.type == "private")
async def order_step(message: Message, state: FSMContext):
    data = await state.get_data()
    step = data.get("step", 0)

    if STEP_ORDER[step] == OrderForm.color:
        # Цвет выбирается только кнопками, текст на этом шаге игнорируем.
        await message.answer(
            "Пожалуйста, выберите цвет кнопкой ниже.", reply_markup=_step_keyboard(step)
        )
        return

    field = STEP_FIELDS[STEP_ORDER[step]]
    await state.update_data(**{field: message.text or ""})
    step += 1

    if step >= len(STEP_ORDER):
        await _finish_item(message, state)
        return

    await state.update_data(step=step)
    await state.set_state(STEP_ORDER[step])
    await message.answer(STEP_PROMPTS[STEP_ORDER[step]], reply_markup=_step_keyboard(step))


async def _finish_item(message: Message, state: FSMContext):
    """Сохраняет заполненный товар в список и предлагает добавить следующий."""
    data = await state.get_data()
    items = list(data.get("items", []))
    items.append({f: data.get(f, "") for f in ITEM_FIELDS})
    await state.update_data(items=items, **{f: "" for f in ITEM_FIELDS})
    await _show_more_prompt(message, state)


async def _show_more_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", [])
    rows = []
    if len(items) < MAX_ITEMS:
        text = (
            f"Товар {len(items)} добавлен в заказ.\n"
            f"Хотите добавить ещё один товар? (всего можно до {MAX_ITEMS})"
        )
        rows.append([InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="item_add")])
    else:
        text = f"Добавлено максимальное количество товаров ({MAX_ITEMS}). Переходим к проверке заказа."
    rows.append([InlineKeyboardButton(text="✅ Перейти к проверке", callback_data="item_done")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="order_back")])
    await state.set_state(OrderForm.more)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "item_add", OrderForm.more)
async def item_add(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", [])
    if len(items) >= MAX_ITEMS:
        await callback.answer("Достигнуто максимальное количество товаров.", show_alert=True)
        return
    step = FIRST_ITEM_STEP
    await state.update_data(step=step)
    await state.set_state(STEP_ORDER[step])
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        f"Товар {len(items) + 1}.\n\n" + STEP_PROMPTS[STEP_ORDER[step]],
        reply_markup=_step_keyboard(step),
    )
    await callback.answer()


@dp.callback_query(F.data == "item_done", OrderForm.more)
async def item_done(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup()
    await _show_summary(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "order_back", OrderForm.more)
async def order_back_from_more(callback: CallbackQuery, state: FSMContext):
    """Назад с вопроса «добавить ещё товар?»: возвращаемся к вопросу про модель последнего товара."""
    data = await state.get_data()
    items = list(data.get("items", []))
    if not items:
        await callback.answer()
        return
    last = items.pop()
    step = len(STEP_ORDER) - 1
    await state.update_data(items=items, step=step, **last)
    await state.set_state(STEP_ORDER[step])
    await callback.message.edit_reply_markup()
    await callback.message.answer(STEP_PROMPTS[STEP_ORDER[step]], reply_markup=_step_keyboard(step))
    await callback.answer()


@dp.message(StateFilter(OrderForm.more, OrderForm.confirm), F.chat.type == "private")
async def order_use_buttons(message: Message):
    await message.answer(
        "Пожалуйста, воспользуйтесь кнопками под сообщением выше. "
        "Чтобы прервать оформление, напишите /cancel."
    )


@dp.callback_query(F.data.startswith("color:"), OrderForm.color)
async def order_color_pick(callback: CallbackQuery, state: FSMContext):
    color = callback.data.split(":", 1)[1]
    data = await state.get_data()
    step = data.get("step", 0)
    await state.update_data(color=color)
    step += 1
    await callback.message.edit_reply_markup()

    # Присылаем страницу каталога с выбранным цветом (там же видны диоптрии по моделям).
    filename = COLOR_CATALOG_FILES.get(color)
    if filename:
        # Ищем картинку в папке catalog, а если её там нет — рядом с bot.py.
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(CATALOG_DIR, filename),
            os.path.join(base_dir, filename),
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if path:
            photo = FSInputFile(path)
        else:
            # Файла нет на диске сервера — берём картинку по ссылке из репозитория GitHub
            # (репозиторий должен быть публичным).
            photo = GITHUB_RAW_BASE + quote(filename)
            try:
                logging.warning(
                    "Файл %s не найден на диске (%s), содержимое папки: %s. "
                    "Отправляю по ссылке GitHub.",
                    filename,
                    candidates,
                    sorted(os.listdir(base_dir))[:30],
                )
            except OSError:
                pass
        try:
            await callback.message.answer_photo(
                photo,
                caption=(
                    f"Каталог: {color}.\n"
                    "Диоптрии, доступные для каждой модели, указаны под названием."
                ),
            )
        except (FileNotFoundError, TelegramAPIError) as e:
            logging.error("Не удалось отправить каталог %s: %s", filename, _safe_err(e))

    if step >= len(STEP_ORDER):
        await _show_summary(callback.message, state)
    else:
        await state.update_data(step=step)
        await state.set_state(STEP_ORDER[step])
        await callback.message.answer(
            STEP_PROMPTS[STEP_ORDER[step]], reply_markup=_step_keyboard(step)
        )
    await callback.answer()


@dp.callback_query(F.data == "order_back", StateFilter(*STEP_ORDER))
async def order_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    step = data.get("step", 0)
    if STEP_ORDER[step] == OrderForm.left_eye and data.get("items"):
        await callback.message.edit_reply_markup()
        await _show_more_prompt(callback.message, state)
        await callback.answer()
        return
    if step == 0:
        await callback.answer("Это первый вопрос анкеты.", show_alert=True)
        return
    step -= 1
    await state.update_data(step=step)
    await state.set_state(STEP_ORDER[step])
    await callback.message.edit_reply_markup()
    await callback.message.answer(STEP_PROMPTS[STEP_ORDER[step]], reply_markup=_step_keyboard(step))
    await callback.answer()


@dp.callback_query(F.data == "order_back", OrderForm.confirm)
async def order_back_from_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup()
    await _show_more_prompt(callback.message, state)
    await callback.answer()


async def _show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    summary = (
        "Проверьте, пожалуйста, заказ:\n\n"
        f"Имя Фамилия: {data['full_name']}\n"
        f"Адрес: {data['address']}\n"
        f"Телефон: {data['phone']}\n\n"
        f"{_format_items(data.get('items', []))}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="order_ok"),
                InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="order_redo"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="order_back")],
        ]
    )
    await state.set_state(OrderForm.confirm)
    await message.answer(summary, reply_markup=keyboard)


@dp.callback_query(F.data == "order_redo", OrderForm.confirm)
async def order_redo(callback: CallbackQuery, state: FSMContext):
    await state.set_data({"step": 0, "items": []})
    await state.set_state(STEP_ORDER[0])
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        "Хорошо, начнём заново.\n\n" + STEP_PROMPTS[STEP_ORDER[0]]
    )
    await callback.answer()


@dp.callback_query(F.data == "order_ok", OrderForm.confirm)
async def order_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = callback.from_user
    code = _client_creator_code(user.id)
    creator = db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (code,)
    ).fetchone()

    items = data.get("items", [])
    first = items[0] if items else {f: "" for f in ITEM_FIELDS}
    db.execute(
        "INSERT INTO orders(client_id, creator_code, full_name, address, phone, "
        "left_eye, right_eye, color, model) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            user.id,
            code,
            data["full_name"],
            data["address"],
            data["phone"],
            first["left_eye"],
            first["right_eye"],
            first["color"],
            first["model"],
        ),
    )
    order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for it in items:
        db.execute(
            "INSERT INTO order_items(order_id, left_eye, right_eye, color, model) "
            "VALUES(?,?,?,?,?)",
            (order_id, it["left_eye"], it["right_eye"], it["color"], it["model"]),
        )
    db.commit()

    await state.clear()
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        "Спасибо! Заказ передан, мы свяжемся с вами по нему в этом чате.\n"
        "Если остались вопросы, нажмите кнопку ниже или напишите /operator.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Связаться с оператором",
                        callback_data=f"operator:{order_id}",
                    )
                ]
            ]
        ),
    )

    if creator and creator[0]:
        text = _order_card_text(order_id, data, code, "new", items)
        try:
            header = await bot.send_message(
                creator[0], text, reply_markup=_order_status_keyboard(order_id)
            )
            db.execute(
                "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) "
                "VALUES(?, ?, ?)",
                (creator[0], header.message_id, user.id),
            )
            db.commit()
        except TelegramAPIError as e:
            logging.error("Не удалось отправить заказ в группу: %s", _safe_err(e))

    await callback.answer()


# ---------- Статусы заказа (кнопки на карточке в группе) ----------
STATUS_LABELS = {
    "new": "🆕 Новый",
    "paid": "💰 Оплачен",
    "shipped": "📦 Отправлен",
    "cancelled": "❌ Отменён",
}


def _order_card_text(order_id, data, code, status, items):
    return (
        f"{STATUS_LABELS.get(status, status)} · заказ №{order_id}\n"
        f"Клиент: {data['full_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Адрес: {data['address']}\n\n"
        f"{_format_items(items)}\n\n"
        f"Код креатора: {code}"
    )


def _load_items(order_id, legacy):
    """Товары заказа из базы; для старых заказов (без order_items) — из колонок orders."""
    rows = db.execute(
        "SELECT left_eye, right_eye, color, model FROM order_items "
        "WHERE order_id = ? ORDER BY id",
        (order_id,),
    ).fetchall()
    source = rows if rows else [legacy]
    return [dict(zip(ITEM_FIELDS, r)) for r in source]


def _order_status_keyboard(order_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Оплачен", callback_data=f"status:{order_id}:paid"
                ),
                InlineKeyboardButton(
                    text="📦 Отправлен", callback_data=f"status:{order_id}:shipped"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменён", callback_data=f"status:{order_id}:cancelled"
                ),
            ],
        ]
    )


@dp.callback_query(F.data.startswith("status:"))
async def order_status_change(callback: CallbackQuery):
    _, order_id, new_status = callback.data.split(":")
    order_id = int(order_id)

    member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
    if member.status not in ("creator", "administrator"):
        await callback.answer("Менять статус может только админ группы.", show_alert=True)
        return

    row = db.execute(
        "SELECT client_id, creator_code, full_name, address, phone, left_eye, "
        "right_eye, color, model FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    if not row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    db.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
    db.commit()

    data = {
        "full_name": row[2],
        "address": row[3],
        "phone": row[4],
    }
    items = _load_items(order_id, row[5:9])
    text = _order_card_text(order_id, data, row[1], new_status, items)
    await callback.message.edit_text(text, reply_markup=_order_status_keyboard(order_id))
    await callback.answer(f"Статус обновлён: {STATUS_LABELS[new_status]}")


# ---------- Любое сообщение клиента -> в группу его креатора ----------
@dp.message(F.chat.type == "private", StateFilter(None))
async def from_client(message: Message):
    if message.text and message.text.startswith("/"):
        return  # прочие команды не пересылаем

    user = message.from_user
    client = db.execute(
        "SELECT creator_code FROM clients WHERE user_id = ?", (user.id,)
    ).fetchone()
    if not client:
        await message.answer(
            "Чтобы связаться с нами, перейдите по персональной ссылке вашего креатора."
        )
        return

    code = client[0]
    creator = db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (code,)
    ).fetchone()
    if not creator or not creator[0]:
        await message.answer("Чат пока не подключён. Попробуйте чуть позже.")
        return
    group_id = creator[0]

    name = user.full_name + (f" (@{user.username})" if user.username else "")
    try:
        header = await bot.send_message(
            group_id, f"👤 {name}\nID: {user.id} · код: {code}"
        )
        copy = await bot.copy_message(
            group_id,
            message.chat.id,
            message.message_id,
            reply_to_message_id=header.message_id,
        )
    except TelegramAPIError as e:
        logging.error("Не удалось отправить в группу %s: %s", group_id, _safe_err(e))
        await message.answer("Не удалось отправить сообщение. Попробуйте позже.")
        return

    # Запоминаем, чьи это сообщения, чтобы реплай в группе дошёл до клиента
    for msg_id in (header.message_id, copy.message_id):
        db.execute(
            "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) "
            "VALUES(?, ?, ?)",
            (group_id, msg_id, user.id),
        )
    db.commit()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
