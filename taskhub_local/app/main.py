from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from dotenv import load_dotenv
from os import getenv
from .db import init_db, connect
from .auth import verify_telegram_init_data

ROOT = Path(__file__).resolve().parent
load_dotenv()
app = FastAPI(title="TaskHub MVP")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "service": "TaskHub"}

@app.post("/api/auth")
def auth(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    token = getenv("BOT_TOKEN", "")
    user_data = verify_telegram_init_data(x_telegram_init_data, token)
    if not user_data and getenv("DEV_MODE", "false").lower() == "true":
        user_data = {"id": 999000, "first_name": "Dev", "username": "local_dev"}
    if not user_data:
        raise HTTPException(401, "Telegram authentication failed")
    admins = {int(x.strip()) for x in getenv("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip().isdigit()}
    role = "admin" if user_data.get("id") in admins or user_data.get("id") == 999000 else "helper"
    with connect() as db:
        db.execute("""INSERT INTO users(telegram_id, username, first_name, role)
        VALUES (?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name""",
        (user_data.get("id"), user_data.get("username"), user_data.get("first_name", ""), role))
        row = db.execute("SELECT * FROM users WHERE telegram_id=?", (user_data.get("id"),)).fetchone()
    return {"user": dict(row), "dev_mode": getenv("DEV_MODE", "false").lower() == "true"}
