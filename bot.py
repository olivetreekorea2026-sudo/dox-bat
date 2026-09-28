import asyncio
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

# Токен бота берётся из переменной окружения (её задаём в Railway, не в коде!)
TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "bot.db")

bot = Bot(TOKEN)
dp = Dispatcher()

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
            "Напишите ваше сообщение, и мы ответим прямо здесь."
        )
        return

    known = db.execute(
        "SELECT 1 FROM clients WHERE user_id = ?", (user_id,)
    ).fetchone()
    if known:
        await message.answer("Вы уже на связи, просто напишите сообщение.")
    else:
        await message.answer(
            "Чтобы связаться с нами, перейдите по персональной ссылке вашего креатора."
        )


# ---------- Любое сообщение клиента -> в группу его креатора ----------
@dp.message(F.chat.type == "private")
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


if __name__ == "__main__":
    asyncio.run(main())
