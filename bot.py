"""DOX Global Telegram bot launcher for Silly Development.

Upload this file to the same GitHub folder as the existing bot.py.
Set APP PY FILE to app.py in the Silly Development Startup panel.

Keep the Telegram token SECRET. Do not put it in GitHub.
Set BOT_TOKEN in the server environment, or create /home/container/.env:
    BOT_TOKEN=your-real-token-from-BotFather

The existing bot.py, images, and requirements.txt remain unchanged.
"""
# DOX Global deployment sync
import os
import runpy
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

# A private file on the server, never a file in the public GitHub repository.
# This small reader needs no extra packages, so it works with the bot's
# existing requirements.txt.
if not os.environ.get("BOT_TOKEN"):
    env_file = BASE_DIR / ".env"
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "BOT_TOKEN":
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ["BOT_TOKEN"] = value
                break

if not os.environ.get("BOT_TOKEN"):
    raise SystemExit(
        "BOT_TOKEN is missing. Create the private .env file in the Silly "
        "Development Files panel. Do not upload the token to GitHub."
    )

# Keep the new orders in a known location on the server.
os.environ.setdefault("DB_PATH", str(BASE_DIR / "bot.db"))
os.chdir(BASE_DIR)

original_bot = BASE_DIR / "bot.py"
if not original_bot.is_file():
    raise SystemExit("bot.py not found next to app.py")

runpy.run_path(str(original_bot), run_name="__main__")
