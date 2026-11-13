"""Launch DOX Global and DOX Korea on ONE free Silly Development server.

Put this file next to the *existing* DOX Global app.py and bot.py; put korea_bot.py, korea_catalog.py and korea_images/ next to them. Set APP PY FILE = run_both.py. No change to
DOX Global app.py / bot.py is needed.
"""
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def validate_tokens():
    global_token = os.environ.get("BOT_TOKEN", "").strip()
    korea_token = os.environ.get("KOREA_BOT_TOKEN", "").strip()
    if not global_token or not korea_token:
        raise RuntimeError("Both BOT_TOKEN and KOREA_BOT_TOKEN are required")
    if global_token == korea_token:
        raise RuntimeError("DOX Global and DOX Korea must use different Telegram bot tokens")


async def main():
    # Imports after token validation so a half-configured project cannot start.
    validate_tokens()
    import app as global_app  # Imports existing DOX Global handlers; __main__ does not execute
    import korea_bot as korea_app
    await asyncio.gather(global_app.dox.main(), korea_app.main())


if __name__ == "__main__":
    asyncio.run(main())
