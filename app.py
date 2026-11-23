

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




