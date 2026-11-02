import os
import runpy
from pathlib import Path
from dotenv import load_dotenv

folder = Path(__file__).resolve().parent
load_dotenv(folder / ".env")

if not os.environ.get("BOT_TOKEN"):
    raise SystemExit("BOT_TOKEN not found. Add it to a private server .env file.")

os.environ.setdefault("DB_PATH", str(folder / "bot.db"))
os.chdir(folder)
runpy.run_path(str(folder / "bot.py"), run_name="__main__")
