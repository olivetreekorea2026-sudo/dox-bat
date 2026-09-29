import asyncio
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

# Токен бота берётся из переменной окружения (её задаём в Railway, не в коде!)
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "bot.db")

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
    qty = State()
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
    qty TEXT,
    status TEXT DEFAULT 'new',       -- new / paid / shipped
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
)
db.commit()

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


@dp.message(Command("order"), F.chat.type == "private")
async def start_order(message: Message, state: FSMContext):
    if not _client_creator_code(message.from_user.id):
        await message.answer(
            "Чтобы оформить заказ, перейдите по персональной ссылке вашего креатора."
        )
        return
    await state.set_state(OrderForm.name)
    await message.answer(
        "Оформляем заказ. В любой момент можно написать /cancel, чтобы прервать.\n\n"
        "Как вас зовут? (ФИО)"
    )


@dp.message(OrderForm.name, F.chat.type == "private")
async def order_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text or "")
    await state.set_state(OrderForm.address)
    await message.answer("Страна и полный адрес доставки?")


@dp.message(OrderForm.address, F.chat.type == "private")
async def order_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text or "")
    await state.set_state(OrderForm.phone)
    await message.answer("Номер телефона для доставки?")


@dp.message(OrderForm.phone, F.chat.type == "private")
async def order_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text or "")
    await state.set_state(OrderForm.left_eye)
    await message.answer("Диоптрии, левый глаз?")


@dp.message(OrderForm.left_eye, F.chat.type == "private")
async def order_left_eye(message: Message, state: FSMContext):
    await state.update_data(left_eye=message.text or "")
    await state.set_state(OrderForm.right_eye)
    await message.answer("Диоптрии, правый глаз?")


@dp.message(OrderForm.right_eye, F.chat.type == "private")
async def order_right_eye(message: Message, state: FSMContext):
    await state.update_data(right_eye=message.text or "")
    await state.set_state(OrderForm.color)
    await message.answer("Цвет линз?")


@dp.message(OrderForm.color, F.chat.type == "private")
async def order_color(message: Message, state: FSMContext):
    await state.update_data(color=message.text or "")
    await state.set_state(OrderForm.qty)
    await message.answer("Количество пар?")


@dp.message(OrderForm.qty, F.chat.type == "private")
async def order_qty(message: Message, state: FSMContext):
    await state.update_data(qty=message.text or "")
    data = await state.get_data()
    summary = (
        "Проверьте, пожалуйста, заказ:\n\n"
        f"ФИО: {data['full_name']}\n"
        f"Адрес: {data['address']}\n"
        f"Телефон: {data['phone']}\n"
        f"Диоптрии: {data['left_eye']} / {data['right_eye']}\n"
        f"Цвет: {data['color']}\n"
        f"Количество пар: {data['qty']}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="order_ok"),
                InlineKeyboardButton(text="✏️ Заполнить заново", callback_data="order_redo"),
            ]
        ]
    )
    await state.set_state(OrderForm.confirm)
    await message.answer(summary, reply_markup=keyboard)


@dp.callback_query(F.data == "order_redo", OrderForm.confirm)
async def order_redo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.name)
    await callback.message.edit_reply_markup()
    await callback.message.answer("Хорошо, начнём заново. Как вас зовут? (ФИО)")
    await callback.answer()


@dp.callback_query(F.data == "order_ok", OrderForm.confirm)
async def order_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user = callback.from_user
    code = _client_creator_code(user.id)
    creator = db.execute(
        "SELECT group_id FROM creators WHERE code = ?", (code,)
    ).fetchone()

    db.execute(
        "INSERT INTO orders(client_id, creator_code, full_name, address, phone, "
        "left_eye, right_eye, color, qty) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            user.id,
            code,
            data["full_name"],
            data["address"],
            data["phone"],
            data["left_eye"],
            data["right_eye"],
            data["color"],
            data["qty"],
        ),
    )
    order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    await state.clear()
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        "Спасибо! Заказ передан, мы свяжемся с вами по нему в этом чате."
    )

    if creator and creator[0]:
        text = (
            f"🆕 Новый заказ №{order_id}\n"
            f"Клиент: {data['full_name']} (id {user.id})\n"
            f"Телефон: {data['phone']}\n"
            f"Адрес: {data['address']}\n"
            f"Диоптрии: {data['left_eye']} / {data['right_eye']}\n"
            f"Цвет: {data['color']}\n"
            f"Количество пар: {data['qty']}\n"
            f"Код креатора: {code}"
        )
        try:
            header = await bot.send_message(creator[0], text)
            db.execute(
                "INSERT OR REPLACE INTO msg_map(group_id, message_id, client_id) "
                "VALUES(?, ?, ?)",
                (creator[0], header.message_id, user.id),
            )
            db.commit()
        except TelegramAPIError as e:
            logging.error("Не удалось отправить заказ в группу: %s", e)

    await callback.answer()


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
        logging.error("Не удалось отправить в группу %s: %s", group_id, e)
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


if _name_ == "_main_":
    asyncio.run(main())
