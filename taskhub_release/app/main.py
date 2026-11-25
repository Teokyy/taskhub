import asyncio
import logging
from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import verify_telegram_init_data
from .db import connect, init_db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("taskhub")
ROOT = Path(__file__).resolve().parent
_bot_task = None
_bot = None
_bot_status = "starting"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_task, _bot, _bot_status
    log.info("TASKHUB_API_STARTING")
    init_db()
    try:
        from .bot import start_bot
        _bot, _bot_task = await start_bot()
        _bot_status = "running" if _bot_task else "disabled"
    except Exception:
        _bot_status = "error"
        log.exception("TASKHUB_TELEGRAM_STARTUP_ERROR")
    log.info("TASKHUB_API_STARTED bot_status=%s", _bot_status)
    try:
        yield
    finally:
        if _bot_task:
            _bot_task.cancel()
            try:
                await _bot_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("TELEGRAM_POLLING_SHUTDOWN_ERROR")
        if _bot:
            await _bot.session.close()
        log.info("TASKHUB_SHUTDOWN_COMPLETE")


app = FastAPI(title="TaskHub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=10000)
    category: str = Field(default="Другое", min_length=1, max_length=60)
    reward_rub: int = Field(ge=0, le=1000000)
    deadline: str | None = Field(default=None, max_length=40)


class SubmitIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ReviewIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)
    comment: str = Field(default="", max_length=2000)


def current_user(init_data: str):
    token = getenv("BOT_TOKEN", "").strip()
    telegram_user = verify_telegram_init_data(init_data, token)
    dev_mode = getenv("DEV_MODE", "false").lower() == "true"
    if not telegram_user and dev_mode:
        telegram_user = {"id": 999000, "first_name": "Разработчик", "username": "dev"}
    if not telegram_user:
        raise HTTPException(401, "Откройте TaskHub через Telegram")

    telegram_id = int(telegram_user["id"])
    admin_ids = {
        int(value.strip())
        for value in getenv("ADMIN_TELEGRAM_IDS", "").split(",")
        if value.strip().isdigit()
    }
    role = "admin" if telegram_id in admin_ids or (dev_mode and telegram_id == 999000) else "helper"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users(telegram_id, username, first_name, role)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name, role=excluded.role
            """,
            (telegram_id, telegram_user.get("username"), telegram_user.get("first_name", ""), role),
        )
        return dict(conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone())


def user(init_data: str):
    return current_user(init_data)


def admin(account):
    if account["role"] != "admin":
        raise HTTPException(403, "Только для администратора")


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "TaskHub", "telegram_bot": _bot_status}


@app.get("/api/me")
def me(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop), 0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        counts = {row["status"]: row["n"] for row in conn.execute("SELECT status, COUNT(*) n FROM tasks WHERE assignee_id=? GROUP BY status", (account["id"],))}
    return {"user": account, "balance_kop": balance, "counts": counts}


@app.get("/api/tasks")
def tasks(status: str = "available", x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        if status == "mine":
            rows = conn.execute("SELECT * FROM tasks WHERE assignee_id=? ORDER BY id DESC", (account["id"],)).fetchall()
        elif status == "all":
            admin(account)
            rows = conn.execute("SELECT t.*, u.first_name assignee_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id ORDER BY t.id DESC").fetchall()
        elif status == "available":
            rows = conn.execute("SELECT * FROM tasks WHERE status='available' ORDER BY id DESC").fetchall()
        elif status in {"in_progress", "submitted", "revision", "accepted", "rejected", "cancelled", "not_completed"}:
            admin(account)
            rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            raise HTTPException(400, "Неизвестный фильтр задач")
    return [dict(row) for row in rows]


@app.post("/api/tasks")
def create_task(data: TaskIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    admin(account)
    if not data.title.strip() or not data.category.strip():
        raise HTTPException(422, "Название и категория не должны быть пустыми")
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks(title, description, category, reward_kop, created_by, deadline) VALUES(?,?,?,?,?,?)",
            (data.title.strip(), data.description.strip(), data.category.strip(), data.reward_rub * 100, account["id"], data.deadline),
        )
        conn.execute("INSERT INTO audit(actor_id, action, object_type, object_id) VALUES(?,?,?,?)", (account["id"], "create", "task", cursor.lastrowid))
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.post("/api/tasks/{task_id}/claim")
def claim(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if account["role"] == "admin":
        raise HTTPException(403, "Администратор не может брать задачи исполнителя")
    with connect() as conn:
        cursor = conn.execute("UPDATE tasks SET status='in_progress', assignee_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'", (account["id"], task_id))
        if cursor.rowcount != 1:
            raise HTTPException(409, "Задача уже занята или недоступна")
        conn.execute("INSERT INTO audit(actor_id, action, object_type, object_id) VALUES(?,?,?,?)", (account["id"], "claim", "task", task_id))
    return {"ok": True}


@app.post("/api/tasks/{task_id}/submit")
def submit(task_id: int, data: SubmitIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if not data.body.strip():
        raise HTTPException(422, "Добавь описание результата или ссылку")
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["assignee_id"] != account["id"] or task["status"] not in ("in_progress", "revision"):
            raise HTTPException(409, "Нельзя сдать эту задачу")
        cursor = conn.execute("INSERT INTO submissions(task_id, user_id, body) VALUES(?,?,?)", (task_id, account["id"], data.body.strip()))
        conn.execute("UPDATE tasks SET status='submitted', updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
        conn.execute("INSERT INTO audit(actor_id, action, object_type, object_id, details) VALUES(?,?,?,?,?)", (account["id"], "submit", "submission", cursor.lastrowid, f"task_id={task_id}"))
    return {"ok": True}


@app.get("/api/review")
def review(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    admin(account)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.*, u.first_name assignee_name, s.body submission, s.created_at submitted_at
            FROM tasks t JOIN users u ON u.id=t.assignee_id
            LEFT JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE task_id=t.id)
            WHERE t.status='submitted' ORDER BY t.id
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/tasks/{task_id}/review")
def decide(task_id: int, data: ReviewIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    admin(account)
    if data.decision not in ("accept", "revision", "reject"):
        raise HTTPException(400, "Неизвестное решение")
    if data.decision in ("revision", "reject") and not data.comment.strip():
        raise HTTPException(400, "Для доработки или отклонения укажите причину")
    status = {"accept": "accepted", "revision": "revision", "reject": "rejected"}[data.decision]
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND status='submitted'", (task_id,)).fetchone()
        if not task:
            raise HTTPException(409, "Работа уже рассмотрена или не найдена")
        cursor = conn.execute("UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='submitted'", (status, task_id))
        if cursor.rowcount != 1:
            raise HTTPException(409, "Работа уже рассмотрена")
        if data.decision == "accept":
            conn.execute(
                "INSERT INTO ledger(user_id, task_id, amount_kop, kind, note) VALUES(?,?,?,?,?)",
                (task["assignee_id"], task_id, task["reward_kop"], "task_earning", data.comment.strip() or "Задача принята"),
            )
        conn.execute("INSERT INTO audit(actor_id, action, object_type, object_id, details) VALUES(?,?,?,?,?)", (account["id"], data.decision, "task", task_id, data.comment.strip()))
    return {"ok": True, "status": status}


@app.get("/api/ledger")
def ledger(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        rows = conn.execute("SELECT l.*, t.title FROM ledger l LEFT JOIN tasks t ON t.id=l.task_id WHERE l.user_id=? ORDER BY l.id DESC", (account["id"],)).fetchall()
    return [dict(row) for row in rows]
