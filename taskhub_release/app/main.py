from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
import urllib.request
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from datetime import date
from os import getenv
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .auth import create_taskhub_session, verify_taskhub_session, verify_telegram_init_data
from .db import connect, init_db
from .rooms import (RoomError, create_room as create_room_record, list_rooms, set_work_mode as set_room_work_mode, require_admin as require_room_admin, require_member as require_room_member, require_mode as require_room_mode)
from .room_task_actions import router as room_task_actions_router
from .room_budgets import get_budget, set_budget_limit, reserve_task, release_task

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("taskhub")
ROOT = Path(__file__).resolve().parent
_bot_task = None
_bot = None
_bot_status = "starting"
BASE_RATE_KOP_PER_HOUR = 10000
DIFFICULTY_FACTORS = {1.0, 1.2, 1.5, 1.8, 2.0}
URGENCY_FACTORS = {1.0, 1.1, 1.25, 1.5}
QUALITY_FACTORS = {"acceptable": 0.9, "good": 1.0, "excellent": 1.1, "outstanding": 1.2}


async def expire_overdue_tasks():
    while True:
        try:
            with connect() as conn:
                rows=conn.execute("""SELECT t.id,t.room_id,t.title,u.telegram_id assignee_chat,c.telegram_id creator_chat
                    FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                    LEFT JOIN users c ON c.id=t.created_by
                    WHERE t.deadline IS NOT NULL AND date(t.deadline)<date('now')
                    AND t.status IN ('available','in_progress','revision')""").fetchall()
                for row in rows:
                    cur=conn.execute("""UPDATE tasks SET status='not_completed',updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('available','in_progress','revision')""",(row["id"],))
                    if cur.rowcount:
                        if row["room_id"] is not None:
                            release_task(conn, row["room_id"], row["id"], None, "released")
                        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(NULL,?,?,?,?)",
                            ("deadline_expired","task",row["id"],"Срок выполнения истёк"))
                        await notify_telegram(row["creator_chat"],f"⌛ Срок задачи «{row['title']}» истёк. Статус: не выполнена.")
                        if row["assignee_chat"]:
                            await notify_telegram(row["assignee_chat"],f"⌛ Срок задачи «{row['title']}» истёк. Статус: не выполнена.")
                        await notify_room(row["room_id"], f"⌛ Срок задачи «{row['title']}» истёк. Статус: не выполнена.")
        except Exception:
            log.exception("TASK_DEADLINE_SWEEP_FAILED")
        await asyncio.sleep(60)



async def notify_room(room_id: int | None, message: str):
    """Best-effort notification to the Telegram group linked to a room."""
    if not room_id or not _bot:
        return
    try:
        with connect() as conn:
            link = conn.execute("SELECT chat_id FROM telegram_room_links WHERE room_id=?", (room_id,)).fetchone()
        if link:
            await _bot.send_message(chat_id=link["chat_id"], text=message)
    except Exception:
        log.exception("TELEGRAM_ROOM_NOTIFICATION_FAILED room_id=%s", room_id)


async def notify_telegram(telegram_id: int | None, message: str):
    """Send a best-effort Telegram notification without breaking the task action."""
    if not telegram_id or not _bot:
        log.warning("TELEGRAM_NOTIFICATION_SKIPPED: bot unavailable or recipient missing")
        return
    try:
        await _bot.send_message(chat_id=telegram_id, text=message)
    except Exception:
        log.exception("TELEGRAM_NOTIFICATION_FAILED recipient=%s", telegram_id)


async def category_digest_worker():
    """Send one deduplicated Telegram digest per subscribed user per UTC hour."""
    while True:
        if not _bot:
            await asyncio.sleep(20)
            continue
        now=datetime.now(timezone.utc)
        if now.minute == 0:
            window_start=now.strftime("%Y-%m-%d %H:00:00")
            with connect() as conn:
                subscribers=conn.execute("""SELECT DISTINCT u.id user_id,u.telegram_id
                    FROM users u JOIN subscriptions s ON s.user_id=u.id""").fetchall()
                batches=[]
                for subscriber in subscribers:
                    subscription_rows=conn.execute("""SELECT c.id,c.name,p.name parent_name FROM subscriptions s
                        JOIN categories c ON c.id=s.category_id LEFT JOIN categories p ON p.id=c.parent_id
                        WHERE s.user_id=?""",(subscriber["user_id"],)).fetchall()
                    category_ids=[x["id"] for x in subscription_rows]
                    if not category_ids: continue
                    marks=",".join("?" for _ in category_ids)
                    rows=conn.execute(f"""SELECT t.id,t.title,t.category FROM tasks t
                        JOIN categories c ON c.id=t.category_id
                        WHERE t.room_id IS NULL AND t.status IN ('open','available')
                          AND t.created_at>=? AND (c.id IN ({marks}) OR c.parent_id IN ({marks}))
                        ORDER BY t.created_at DESC LIMIT 30""",
                        [window_start,*category_ids,*category_ids]).fetchall()
                    if not rows: continue
                    cur=conn.execute("INSERT OR IGNORE INTO digest_windows(user_id,window_start) VALUES(?,?)",
                                     (subscriber["user_id"],window_start))
                    if cur.rowcount:
                        batches.append((subscriber["user_id"],subscriber["telegram_id"],[dict(x) for x in rows],[dict(x) for x in subscription_rows]))
            for user_id,telegram_id,rows,subscription_rows in batches:
                lines=[f"• {x['title']} — {x['category']}" for x in rows[:15]]
                message="🆕 Новые задания по твоим подпискам:\n"+"\n".join(lines)
                if len(rows)>15: message+=f"\nИ ещё {len(rows)-15} заданий."
                if _bot:
                    keyboard=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="Отключить: "+(x["parent_name"]+" / " if x["parent_name"] else "")+x["name"],callback_data=f"sub:off:{x['id']}")]
                        for x in subscription_rows
                    ])
                    try:
                        await _bot.send_message(chat_id=telegram_id,text=message,reply_markup=keyboard)
                    except Exception:
                        log.exception("CATEGORY_DIGEST_SEND_FAILED user_id=%s",user_id)
                with connect() as conn:
                    conn.execute("UPDATE digest_windows SET sent_at=CURRENT_TIMESTAMP WHERE user_id=? AND window_start=?",
                                 (user_id,window_start))
        await asyncio.sleep(20)


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
    expiry_task = asyncio.create_task(expire_overdue_tasks(), name="task-deadline-expiry")
    digest_task = asyncio.create_task(category_digest_worker(), name="category-hourly-digest")
    log.info("TASKHUB_API_STARTED bot_status=%s", _bot_status)
    try:
        yield
    finally:
        expiry_task.cancel()
        digest_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
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
app.include_router(room_task_actions_router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.middleware("http")
async def block_legacy_global_api(request, call_next):
    """Keep room data scoped while allowing the independent public marketplace API."""
    path = request.url.path.rstrip("/") or "/"
    # Category catalogue is a global marketplace resource. Bypass the legacy
    # room API guard explicitly before evaluating any legacy-route rules.
    # Keep the raw path in logs so proxy/path-normalization issues are visible.
    if path == "/api/categories" or path.startswith("/api/categories/"):
        log.info(
            "CATEGORY_API_BYPASS method=%s path=%s raw_path=%r",
            request.method, path, request.scope.get("raw_path"),
        )
        return await call_next(request)
    allowed = {
        "/api/me", "/api/health", "/api/session", "/api/onboarding", "/api/categories",
        "/api/presence", "/api/market/profile", "/api/market/tasks", "/api/market/ledger",
    }
    # Onboarding and category endpoints are global account preferences, not legacy room data.
    if path.startswith((
        "/api/onboarding/", "/api/categories/", "/api/category-requests/",
        "/api/market/tasks/", "/api/moderation/disputes/",
    )):
        allowed.add(path)
    if path in ("/api/category-requests", "/api/moderation/disputes"):
        allowed.add(path)
    if path.startswith("/api/") and path not in allowed and not path.startswith(("/api/rooms", "/api/market/")):
        try:
            with connect() as conn:
                migrated = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'"
                ).fetchone() is not None
        except Exception:
            migrated = False
        if migrated:
            log.warning("LEGACY_API_BLOCKED method=%s path=%s", request.method, path)
            return JSONResponse(status_code=410, content={"detail": "Этот адрес относится к старому API. Используйте API комнаты или биржи.", "path": path})
    response = await call_next(request)
    # Telegram WebViews can retain an old Mini App bundle across deployments.
    # Always revalidate the entry point and application bundle so frontend API
    # routes cannot remain out of sync with the backend after a release.
    if path in ("/", "/index.html", "/static/index.html", "/static/app.js"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response







class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RoomModeIn(BaseModel):
    mode: str = Field(pattern="^(customer|executor)$")


class InviteCreateIn(BaseModel):
    expires_in_days: int | None = Field(default=7, ge=1, le=30)
    max_uses: int | None = Field(default=10, ge=1, le=100)


class InviteJoinIn(BaseModel):
    code: str = Field(min_length=20, max_length=100)


class MemberRoleIn(BaseModel):
    room_role: str = Field(pattern="^(admin|member)$")


class RoomBudgetIn(BaseModel):
    limit_rub: int | None = Field(default=None, ge=0, le=1000000000)


class CriterionIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    weight: int = Field(default=0, ge=0, le=100)
    critical: bool = False


class ProjectIn(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=10000)
    budget_rub: int = Field(ge=0, le=100000000)


class StageIn(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=10000)
    budget_rub: int = Field(ge=0, le=100000000)


class TaskIn(BaseModel):
    task_mode: str = Field(default="online", pattern="^(online|irl)$")
    city: str = Field(default="", max_length=120)
    pickup_address: str = Field(default="", max_length=500)
    delivery_address: str = Field(default="", max_length=500)
    pickup_lat: float | None = Field(default=None, ge=-90, le=90)
    pickup_lon: float | None = Field(default=None, ge=-180, le=180)
    delivery_lat: float | None = Field(default=None, ge=-90, le=90)
    delivery_lon: float | None = Field(default=None, ge=-180, le=180)
    parcel_weight_kg: float | None = Field(default=None, gt=0, le=200)
    parcel_length_cm: float | None = Field(default=None, gt=0, le=500)
    parcel_width_cm: float | None = Field(default=None, gt=0, le=500)
    parcel_height_cm: float | None = Field(default=None, gt=0, le=500)
    parcel_count: int = Field(default=1, ge=1, le=50)
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=10000)
    category: str = Field(default="Другое", min_length=1, max_length=120)
    category_id: int | None = None
    other_category_text: str | None = Field(default=None, max_length=200)
    reward_rub: int | None = Field(default=None, ge=0, le=1000000)
    pricing_model: str = Field(default="fixed")
    norm_minutes: int = Field(default=0, ge=0, le=100000)
    difficulty: float = Field(default=1.0)
    urgency: float = Field(default=1.0)
    project_id: int | None = None
    stage_id: int | None = None
    tour_id: int | None = None
    deadline: str | None = Field(default=None, max_length=40)
    criteria: list[CriterionIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pricing(self):
        if self.task_mode == "irl" and not self.city.strip():
            raise ValueError("Для задания IRL укажи город")
        if self.task_mode == "online" and any((self.pickup_address, self.delivery_address)):
            raise ValueError("Адреса доставки доступны только для IRL-заданий")
        if self.pricing_model not in {"fixed", "normative"}:
            raise ValueError("Модель оплаты: fixed или normative")
        if self.difficulty not in DIFFICULTY_FACTORS:
            raise ValueError("Недопустимый коэффициент сложности")
        if self.urgency not in URGENCY_FACTORS:
            raise ValueError("Недопустимый коэффициент срочности")
        if self.pricing_model == "normative" and self.norm_minutes <= 0:
            raise ValueError("Для нормативной оплаты укажи нормативное время")
        if self.pricing_model == "fixed" and self.reward_rub is None:
            raise ValueError("Для фиксированной оплаты укажи сумму")
        if self.criteria:
            total = sum(item.weight for item in self.criteria)
            if total not in (0, 100):
                raise ValueError("Вес критериев должен составлять 100% или 0% для неделимой задачи")
        return self



class CategoryCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    parent_id: int | None = None


class CategoryUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    parent_id: int | None = None
    is_hidden: bool | None = None


class CategoryRequestResolveIn(BaseModel):
    status: str = Field(pattern="^(added|moved|rejected)$")
    category_id: int | None = None
    note: str = Field(default="", max_length=1000)


class SubscriptionUpdateIn(BaseModel):
    category_ids: list[int] = Field(default_factory=list, max_length=20)


class OnboardingUpdateIn(BaseModel):
    role_flags: str | None = Field(default=None, pattern="^(customer|executor|both)$")
    completed: bool | None = None
    skipped: bool | None = None


class DeliveryQuoteIn(BaseModel):
    start_lat: float = Field(ge=-90, le=90)
    start_lon: float = Field(ge=-180, le=180)


class TaskAttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=180)
    mime_type: str = Field(pattern="^(image/jpeg|image/png|image/webp)$")
    data_base64: str = Field(min_length=1, max_length=14_000_000)


class CourierProfileIn(BaseModel):
    city: str = Field(min_length=2, max_length=120)
    transport: str = Field(default="walk", pattern="^(walk|bicycle|car|motorcycle)$")
    max_weight_kg: float = Field(default=5, gt=0, le=200)
    max_length_cm: float = Field(default=40, gt=0, le=500)
    max_width_cm: float = Field(default=30, gt=0, le=500)
    max_height_cm: float = Field(default=30, gt=0, le=500)
    max_items: int = Field(default=1, ge=1, le=50)


class MarketResponseIn(BaseModel):
    message: str = Field(default="", max_length=1000)
    proposed_deadline: str | None = Field(default=None, max_length=40)


class MarketReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: str = Field(default="", max_length=500)


class DisputeResolutionIn(BaseModel):
    decision: str = Field(pattern="^(done|cancelled|reopen)$")
    comment: str = Field(default="", max_length=2000)


class MarketDisputeIn(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


class MarketServiceIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=10000)
    category: str = Field(min_length=1, max_length=100)
    price_rub: int = Field(ge=1, le=1000000)
    delivery_minutes: int = Field(default=60, ge=1, le=100000)


class MarketServiceOrderIn(BaseModel):
    details: str = Field(default="", max_length=3000)


class MarketServiceOrderStatusIn(BaseModel):
    status: str = Field(pattern="^(accepted|delivered|completed|cancelled)$")


class MarketTaskUpdate(TaskIn):
    pass


class MarketNotificationPrefs(BaseModel):
    categories: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True


class SubmitIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ReviewIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)
    comment: str = Field(default="", max_length=2000)
    quality: str = Field(default="good")

class ModerationIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="",max_length=2000)

def ensure_rooms_schema(conn):
    required = {"rooms", "room_members", "room_payout_settings", "room_invites"}
    found = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if required.issubset(found):
        return

    # A genuinely fresh installation can bootstrap the room schema in place.
    # Never auto-migrate a populated legacy workspace: that requires the
    # explicit backup-gated migration utility.
    legacy_tables = ("projects", "stages", "tours", "tasks", "submissions", "ledger", "payouts")
    for table in legacy_tables:
        if conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
            raise HTTPException(503, "Обнаружены старые данные. Для них нужна отдельная проверенная миграция.")
    from scripts.migrate_to_rooms import create_schema, SCHEMA_VERSION
    create_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,name) VALUES(?,?)",
        (SCHEMA_VERSION, "fresh_install_rooms_schema"),
    )


def room_error(error: RoomError):
    raise HTTPException(error.status_code, str(error))



@app.get("/api/rooms/{room_id}/telegram")
def get_room_telegram(room_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        row = conn.execute("SELECT chat_title,connected_at FROM telegram_room_links WHERE room_id=?", (room_id,)).fetchone()
        return {"connected": bool(row), "chat_title": row["chat_title"] if row else None, "connected_at": row["connected_at"] if row else None}


@app.post("/api/rooms/{room_id}/telegram/code")
def create_room_telegram_code(room_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        token = secrets.token_urlsafe(24)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        conn.execute("""INSERT INTO telegram_room_link_codes(room_id,code_hash,expires_at,created_by)
                        VALUES(?,?,?,?) ON CONFLICT(room_id) DO UPDATE SET
                        code_hash=excluded.code_hash,expires_at=excluded.expires_at,created_by=excluded.created_by""",
                     (room_id, hashlib.sha256(token.encode()).hexdigest(), expires_at, account["id"]))
        return {"code": token, "expires_at": expires_at}


@app.delete("/api/rooms/{room_id}/telegram")
def disconnect_room_telegram(room_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        conn.execute("DELETE FROM telegram_room_links WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM telegram_room_link_codes WHERE room_id=?", (room_id,))
        return {"ok": True}


@app.get("/api/rooms/{room_id}/budget")
def room_budget(room_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
            return get_budget(conn, room_id)
        except RoomError as error:
            room_error(error)


@app.put("/api/rooms/{room_id}/budget")
def update_room_budget(room_id: int, data: RoomBudgetIn,
                       x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
            limit_kop = None if data.limit_rub is None else data.limit_rub * 100
            return set_budget_limit(conn, room_id, account["id"], limit_kop)
        except RoomError as error:
            room_error(error)


@app.get("/api/rooms/{room_id}/tasks")
def room_tasks(room_id: int, status: str = "available",
               x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        if status == "mine":
            rows = conn.execute(
                """SELECT * FROM tasks WHERE room_id=? AND
                   (created_by=? OR assignee_id=?) ORDER BY id DESC""",
                (room_id, account["id"], account["id"]),
            ).fetchall()
        elif status == "available":
            rows = conn.execute(
                """SELECT * FROM tasks WHERE room_id=? AND status='available'
                   AND moderation_status='approved' ORDER BY id DESC""", (room_id,)
            ).fetchall()
        elif status == "all":
            try:
                require_room_admin(conn, room_id, account["id"])
            except RoomError as error:
                room_error(error)
            rows = conn.execute(
                """SELECT t.*,u.first_name assignee_name,cu.first_name creator_name
                   FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                   LEFT JOIN users cu ON cu.id=t.created_by
                   WHERE t.room_id=? ORDER BY t.id DESC""", (room_id,)
            ).fetchall()
        else:
            raise HTTPException(400, "Неизвестный фильтр задач")
        return [dict(row) for row in rows]


@app.get("/api/rooms/{room_id}/projects")
def room_projects(room_id: int,
                 x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        rows = conn.execute(
            """SELECT p.*,
                      COALESCE((SELECT SUM(s.budget_kop) FROM stages s
                                WHERE s.project_id=p.id AND s.room_id=p.room_id),0) stages_budget_kop,
                      p.budget_reserved_kop direct_reserved_kop,
                      p.budget_spent_kop direct_spent_kop,
                      MAX(0,p.budget_kop-COALESCE((SELECT SUM(s.budget_kop) FROM stages s
                                WHERE s.project_id=p.id AND s.room_id=p.room_id),0)
                                -p.budget_reserved_kop-p.budget_spent_kop) direct_available_kop
               FROM projects p WHERE p.room_id=? ORDER BY p.id DESC""",
            (room_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/api/rooms/{room_id}/projects")
def create_room_project(room_id: int, data: ProjectIn,
                        x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        cur = conn.execute(
            """INSERT INTO projects(title,description,budget_kop,created_by,room_id)
               VALUES(?,?,?,?,?)""",
            (data.title.strip(), data.description.strip(), data.budget_rub * 100,
             account["id"], room_id),
        )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "create", "project", cur.lastrowid, f"room_id={room_id}"),
        )
        return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())


@app.get("/api/rooms/{room_id}/projects/{project_id}/stages")
def room_project_stages(room_id: int, project_id: int,
                        x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        project = conn.execute(
            "SELECT id FROM projects WHERE id=? AND room_id=?", (project_id, room_id)
        ).fetchone()
        if not project:
            raise HTTPException(404, "Проект не найден в этой комнате")
        rows = conn.execute(
            "SELECT * FROM stages WHERE project_id=? AND room_id=? ORDER BY position,id",
            (project_id, room_id),
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/api/rooms/{room_id}/projects/{project_id}/stages")
def create_room_stage(room_id: int, project_id: int, data: StageIn,
                      x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        # Serialize allocation check + stage insert against concurrent budget changes.
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT * FROM projects WHERE id=? AND room_id=?", (project_id, room_id)
        ).fetchone()
        if not project:
            raise HTTPException(404, "Проект не найден в этой комнате")
        allocated = conn.execute(
            "SELECT COALESCE(SUM(budget_kop),0) n FROM stages WHERE project_id=? AND room_id=?",
            (project_id, room_id),
        ).fetchone()["n"]
        committed_direct = int(project["budget_reserved_kop"] or 0) + int(project["budget_spent_kop"] or 0)
        if allocated + committed_direct + data.budget_rub * 100 > project["budget_kop"]:
            raise HTTPException(409, "Распределение этапов и прямые расходы превышают бюджет проекта")
        cur = conn.execute(
            """INSERT INTO stages(project_id,title,description,budget_kop,room_id)
               VALUES(?,?,?,?,?)""",
            (project_id, data.title.strip(), data.description.strip(),
             data.budget_rub * 100, room_id),
        )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "create", "stage", cur.lastrowid, f"room_id={room_id}"),
        )
        return dict(conn.execute("SELECT * FROM stages WHERE id=?", (cur.lastrowid,)).fetchone())


@app.get("/api/rooms/{room_id}/team")
def room_team(room_id: int,
              x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        rows = conn.execute(
            """SELECT u.id,u.telegram_id,u.username,u.first_name,u.photo_url,
                      rm.room_role,rm.work_mode,
                      COUNT(CASE WHEN t.status='in_progress' THEN 1 END) active_tasks,
                      COUNT(CASE WHEN t.status='submitted' THEN 1 END) submitted_tasks,
                      COALESCE((SELECT SUM(l.amount_kop) FROM ledger l
                                WHERE l.user_id=u.id AND l.room_id=rm.room_id),0) earned_kop
               FROM room_members rm JOIN users u ON u.id=rm.user_id
               LEFT JOIN tasks t ON t.room_id=rm.room_id AND
                    (t.assignee_id=u.id OR t.created_by=u.id)
               WHERE rm.room_id=? AND rm.left_at IS NULL
               GROUP BY u.id,rm.room_id ORDER BY u.first_name,u.id""",
            (room_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/rooms/{room_id}/moderation")
def room_moderation_queue(room_id: int,
                          x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        rows = conn.execute(
            """SELECT t.*,u.first_name creator_name,u.username creator_username
               FROM tasks t LEFT JOIN users u ON u.id=t.created_by
               WHERE t.room_id=? AND t.status='pending'
                 AND t.moderation_status='pending' ORDER BY t.created_at,t.id""",
            (room_id,),
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/rooms/{room_id}/review")
def room_review_queue(room_id: int,
                      x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_mode(conn, room_id, account["id"], "customer")
        except RoomError as error:
            room_error(error)
        rows = conn.execute(
            """SELECT t.*,u.first_name assignee_name,
                      s.id submission_id,s.body submission,s.created_at submitted_at
               FROM tasks t JOIN users u ON u.id=t.assignee_id
               LEFT JOIN submissions s ON s.id=(
                   SELECT MAX(id) FROM submissions WHERE task_id=t.id
               )
               WHERE t.room_id=? AND t.created_by=? AND t.status='submitted'
               ORDER BY t.id""",
            (room_id, account["id"]),
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/rooms/{room_id}/ledger")
def room_ledger(room_id: int,
                x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        balance = conn.execute(
            "SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE room_id=? AND user_id=?",
            (room_id, account["id"]),
        ).fetchone()["n"]
        rows = conn.execute(
            """SELECT l.*,t.title FROM ledger l LEFT JOIN tasks t ON t.id=l.task_id
               WHERE l.room_id=? AND l.user_id=? ORDER BY l.id DESC""",
            (room_id, account["id"]),
        ).fetchall()
        return {"balance_kop": balance, "entries": [dict(row) for row in rows]}


@app.post("/api/rooms/{room_id}/tasks")
async def create_room_task(room_id: int, data: TaskIn,
                     x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    reward = task_base_reward(data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_mode(conn, room_id, account["id"], "customer")
        except RoomError as error:
            room_error(error)
        room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not room:
            raise HTTPException(404, "Комната не найдена")
        if data.tour_id is not None:
            raise HTTPException(400, "Привязка задачи к туру пока не поддерживается")
        project_id = data.project_id
        stage_id = data.stage_id
        if stage_id is not None:
            stage = conn.execute(
                "SELECT id,project_id FROM stages WHERE id=? AND room_id=?",
                (stage_id, room_id),
            ).fetchone()
            if not stage:
                raise HTTPException(404, "Этап не найден в этой комнате")
            if project_id is not None and project_id != stage["project_id"]:
                raise HTTPException(422, "Этап не принадлежит выбранному проекту")
            project_id = stage["project_id"]
        if project_id is not None:
            project = conn.execute(
                "SELECT id FROM projects WHERE id=? AND room_id=?",
                (project_id, room_id),
            ).fetchone()
            if not project:
                raise HTTPException(404, "Проект не найден в этой комнате")
        status = "pending" if room["moderation_enabled"] else "available"
        moderation = "pending" if room["moderation_enabled"] else "approved"
        reserve = 0
        cur = conn.execute(
            """INSERT INTO tasks(title,description,category,reward_kop,status,created_by,
               deadline,pricing_model,norm_minutes,difficulty,urgency,fixed_reward_kop,
               budget_reserved_kop,moderation_status,criteria_frozen,room_id,project_id,stage_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.title.strip(), data.description.strip(), data.category.strip(), reward,
             status, account["id"], data.deadline, data.pricing_model, data.norm_minutes,
             data.difficulty, data.urgency, reward, reserve, moderation, 1, room_id,
             project_id, stage_id),
        )
        for pos, item in enumerate(data.criteria):
            conn.execute(
                "INSERT INTO task_criteria(task_id,title,weight,critical,position) VALUES(?,?,?,?,?)",
                (cur.lastrowid, item.title.strip(), item.weight, int(item.critical), pos),
            )
        if status == "available":
            try:
                reserve_task(conn, room_id, cur.lastrowid, reward, account["id"])
            except RoomError as error:
                room_error(error)
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?, 'created',NULL,?,?)""",
            (cur.lastrowid, account["id"], status, f"room_id={room_id}"),
        )
        created = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())
    await notify_room(room_id, f"🆕 Новая задача: «{created['title']}»" + (" (ожидает модерации)" if status == "pending" else ""))
    return created


@app.post("/api/rooms/{room_id}/tasks/{task_id}/claim")
async def claim_room_task(room_id: int, task_id: int,
                    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_mode(conn, room_id, account["id"], "executor")
        except RoomError as error:
            room_error(error)
        task = conn.execute(
            """SELECT * FROM tasks WHERE id=? AND room_id=? AND status='available'
               AND moderation_status='approved'""", (task_id, room_id)
        ).fetchone()
        if not task:
            raise HTTPException(409, "Задача недоступна")
        if task["created_by"] == account["id"]:
            raise HTTPException(403, "Нельзя взять собственную задачу")
        cur = conn.execute(
            """UPDATE tasks SET status='in_progress',assignee_id=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND room_id=? AND status='available'""",
            (account["id"], task_id, room_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Задача уже занята")
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status)
               VALUES(?,?,'claimed','available','in_progress')""",
            (task_id, account["id"]),
        )
        title = task["title"]
    await notify_room(room_id, f"👀 Задачу «{title}» взяли в работу. Исполнитель: {account.get('first_name') or 'Участник'}")
    return {"ok": True, "status": "in_progress"}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/submit")
async def submit_room_task(room_id: int, task_id: int, data: SubmitIn,
                     x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_mode(conn, room_id, account["id"], "executor")
        except RoomError as error:
            room_error(error)
        task = conn.execute(
            """SELECT * FROM tasks WHERE id=? AND room_id=? AND assignee_id=?
               AND status IN ('in_progress','revision')""",
            (task_id, room_id, account["id"]),
        ).fetchone()
        if not task:
            raise HTTPException(409, "Нельзя сдать эту задачу")
        cur = conn.execute(
            "INSERT INTO submissions(task_id,user_id,body) VALUES(?,?,?)",
            (task_id, account["id"], data.body.strip()),
        )
        conn.execute(
            "UPDATE tasks SET status='submitted',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,),
        )
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?,'submitted',?,'submitted',?)""",
            (task_id, account["id"], task["status"], f"submission_id={cur.lastrowid}"),
        )
        title = task["title"]
        creator = conn.execute("SELECT telegram_id FROM users WHERE id=?", (task["created_by"],)).fetchone()
    await notify_room(room_id, f"📨 Работа по задаче «{title}» отправлена на проверку.")
    if creator:
        await notify_telegram(creator["telegram_id"], f"📨 Работа по твоей задаче «{title}» отправлена на проверку.")
    return {"ok": True, "status": "submitted"}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/moderate")
async def moderate_room_task(room_id: int, task_id: int, data: ModerationIn,
                       x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.decision == "reject" and not data.comment.strip():
        raise HTTPException(400, "Укажи причину отклонения")
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        task = conn.execute(
            """SELECT * FROM tasks WHERE id=? AND room_id=? AND status='pending'
               AND moderation_status='pending'""", (task_id, room_id)
        ).fetchone()
        if not task:
            raise HTTPException(404, "Задача не найдена или уже рассмотрена")
        if data.decision == "approve":
            # Reserve atomically at publication, never while pending.
            try:
                reserve_task(conn, room_id, task_id, int(task["reward_kop"]), account["id"])
            except RoomError as error:
                room_error(error)
            cur = conn.execute(
                """UPDATE tasks SET status='available',moderation_status='approved',
                   budget_reserved_kop=reward_kop,moderation_comment=?,moderated_by=?,
                   moderated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND room_id=? AND status='pending'""",
                (data.comment.strip(), account["id"], task_id, room_id),
            )
            new_status = "available"
        else:
            cur = conn.execute(
                """UPDATE tasks SET status='rejected',moderation_status='rejected',
                   budget_reserved_kop=0,moderation_comment=?,moderated_by=?,
                   moderated_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND room_id=? AND status='pending'""",
                (data.comment.strip(), account["id"], task_id, room_id),
            )
            new_status = "rejected"
        if cur.rowcount != 1:
            raise HTTPException(409, "Задача уже рассмотрена")
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?,?,?,?,?)""",
            (task_id, account["id"], "moderation_" + data.decision,
             "pending", new_status, data.comment.strip()),
        )
        title = task["title"]
    await notify_room(room_id, ("✅ Задача одобрена и опубликована" if data.decision == "approve" else "❌ Задача отклонена") + f"\n«{title}»" + (f"\nПричина: {data.comment.strip()}" if data.comment.strip() else ""))
    return {"ok": True, "status": new_status}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/review")
async def review_room_task(room_id: int, task_id: int, data: ReviewIn,
                     x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.decision not in ("accept", "revision", "reject"):
        raise HTTPException(400, "Неизвестное решение")
    if data.decision in ("revision", "reject") and not data.comment.strip():
        raise HTTPException(400, "Укажи причину")
    if data.quality not in QUALITY_FACTORS:
        raise HTTPException(400, "Неизвестная оценка качества")
    if data.quality != "good" and not data.comment.strip():
        raise HTTPException(400, "Для нестандартной оценки качества укажи обоснование")
    new_status = {"accept": "accepted", "revision": "revision", "reject": "rejected"}[data.decision]
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_mode(conn, room_id, account["id"], "customer")
        except RoomError as error:
            room_error(error)
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND room_id=? AND status='submitted'",
            (task_id, room_id),
        ).fetchone()
        if not task:
            raise HTTPException(409, "Работа уже рассмотрена или не найдена")
        if task["created_by"] != account["id"]:
            raise HTTPException(403, "Проверить работу может только автор задачи")
        cur = conn.execute(
            """UPDATE tasks SET status=?,quality_factor=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND room_id=? AND status='submitted'""",
            (new_status, QUALITY_FACTORS[data.quality], task_id, room_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Работа уже рассмотрена")
        if data.decision == "accept":
            amount = int(round(task["reward_kop"] * QUALITY_FACTORS[data.quality]))
            try:
                release_task(conn, room_id, task_id, account["id"], "spent", amount)
            except RoomError as error:
                room_error(error)
            conn.execute(
                """INSERT INTO ledger(user_id,task_id,room_id,amount_kop,kind,note)
                   VALUES(?,?,?,?,?,?)""",
                (task["assignee_id"], task_id, room_id, amount, "task_earning",
                 f"Принято · качество: {data.quality} · {data.comment.strip()}"),
            )
            conn.execute(
                "UPDATE tasks SET budget_reserved_kop=0 WHERE id=? AND room_id=?",
                (task_id, room_id),
            )
        elif data.decision == "reject":
            release_task(conn, room_id, task_id, account["id"], "released")
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?,?,?,?,?)""",
            (task_id, account["id"], data.decision, "submitted", new_status,
             f"quality={data.quality};{data.comment.strip()}"),
        )
        title = task["title"]
        assignee = conn.execute("SELECT telegram_id FROM users WHERE id=?", (task["assignee_id"],)).fetchone()
    message = {"accepted": "✅ Работа принята", "revision": "🛠 Работа отправлена на доработку", "rejected": "❌ Работа отклонена"}[new_status] + f"\n«{title}»" + (f"\nКомментарий: {data.comment.strip()}" if data.comment.strip() else "")
    await notify_room(room_id, message)
    if assignee:
        await notify_telegram(assignee["telegram_id"], message)
    return {"ok": True, "status": new_status}


@app.get("/api/rooms")
def get_rooms(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        return list_rooms(conn, account["id"])


@app.post("/api/rooms")
def post_room(data: RoomIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            return create_room_record(conn, account["id"], data.name)
        except RoomError as error:
            room_error(error)



@app.post("/api/rooms/{room_id}/invites")
def create_room_invite(room_id: int, data: InviteCreateIn,
                       x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = None
        if data.expires_in_days is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)).isoformat()
        cur = conn.execute(
            "INSERT INTO room_invites(room_id,token_hash,created_by,expires_at,max_uses) VALUES(?,?,?,?,?)",
            (room_id, token_hash, account["id"], expires_at, data.max_uses),
        )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "invite_created", "room_invite", cur.lastrowid, f"room_id={room_id}"),
        )
        return {"id": cur.lastrowid, "code": token, "expires_at": expires_at, "max_uses": data.max_uses}


@app.get("/api/rooms/{room_id}/invites")
def list_room_invites(room_id: int,
                      x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        rows = conn.execute(
            """SELECT id,expires_at,max_uses,uses_count,revoked_at,created_at
               FROM room_invites WHERE room_id=? ORDER BY id DESC LIMIT 30""", (room_id,)
        ).fetchall()
        return [dict(row) for row in rows]


@app.delete("/api/rooms/{room_id}/invites/{invite_id}")
def revoke_room_invite(room_id: int, invite_id: int,
                       x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_admin(conn, room_id, account["id"])
        except RoomError as error:
            room_error(error)
        cur = conn.execute(
            "UPDATE room_invites SET revoked_at=CURRENT_TIMESTAMP WHERE id=? AND room_id=? AND revoked_at IS NULL",
            (invite_id, room_id),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Приглашение не найдено или уже отозвано")
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "invite_revoked", "room_invite", invite_id, f"room_id={room_id}"),
        )
        return {"ok": True}


@app.post("/api/rooms/join")
def join_room(data: InviteJoinIn,
              x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    token_hash = hashlib.sha256(data.code.strip().encode()).hexdigest()
    with connect() as conn:
        ensure_rooms_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        invite = conn.execute(
            """SELECT i.*,r.name room_name FROM room_invites i JOIN rooms r ON r.id=i.room_id
               WHERE i.token_hash=? AND i.revoked_at IS NULL AND r.archived_at IS NULL""",
            (token_hash,),
        ).fetchone()
        if not invite:
            raise HTTPException(404, "Приглашение не найдено или отозвано")
        if invite["expires_at"] and datetime.fromisoformat(invite["expires_at"]) <= datetime.now(timezone.utc):
            raise HTTPException(410, "Срок приглашения истёк")
        if invite["max_uses"] is not None and invite["uses_count"] >= invite["max_uses"]:
            raise HTTPException(410, "Лимит использований приглашения исчерпан")
        member = conn.execute(
            "SELECT * FROM room_members WHERE room_id=? AND user_id=?", (invite["room_id"], account["id"])
        ).fetchone()
        if member and member["left_at"] is None:
            raise HTTPException(409, "Ты уже состоишь в этой комнате")
        if member:
            conn.execute(
                "UPDATE room_members SET room_role='member',work_mode=NULL,left_at=NULL,joined_at=CURRENT_TIMESTAMP WHERE id=?",
                (member["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO room_members(room_id,user_id,room_role,work_mode) VALUES(?,?,'member',NULL)",
                (invite["room_id"], account["id"]),
            )
        conn.execute("UPDATE room_invites SET uses_count=uses_count+1 WHERE id=?", (invite["id"],))
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "invite_joined", "room", invite["room_id"], f"invite_id={invite['id']}"),
        )
        return {"room_id": invite["room_id"], "room_name": invite["room_name"], "work_mode": None}


@app.patch("/api/rooms/{room_id}/members/{member_user_id}")
def update_room_member_role(room_id: int, member_user_id: int, data: MemberRoleIn,
                            x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            actor = require_room_admin(conn, room_id, account["id"])
            target = require_room_member(conn, room_id, member_user_id)
        except RoomError as error:
            room_error(error)
        if target["room_role"] == "owner":
            raise HTTPException(409, "Роль владельца нельзя изменить этим действием")
        if actor["room_role"] != "owner" and (target["room_role"] == "admin" or data.room_role == "admin"):
            raise HTTPException(403, "Назначать и снимать администраторов может только владелец")
        conn.execute(
            "UPDATE room_members SET room_role=? WHERE room_id=? AND user_id=? AND left_at IS NULL",
            (data.room_role, room_id, member_user_id),
        )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "member_role_changed", "room_member", member_user_id,
             f"room_id={room_id};role={data.room_role}"),
        )
        return {"user_id": member_user_id, "room_role": data.room_role}


@app.delete("/api/rooms/{room_id}/members/{member_user_id}")
def remove_room_member(room_id: int, member_user_id: int,
                       x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            actor = require_room_admin(conn, room_id, account["id"])
            target = require_room_member(conn, room_id, member_user_id)
        except RoomError as error:
            room_error(error)
        if target["room_role"] == "owner":
            raise HTTPException(409, "Владельца нельзя удалить из комнаты")
        if actor["room_role"] != "owner" and target["room_role"] != "member":
            raise HTTPException(403, "Администратор может удалить только обычного участника")
        active = conn.execute(
            """SELECT id FROM tasks WHERE room_id=? AND status IN ('in_progress','revision','submitted')
               AND (assignee_id=? OR created_by=?) LIMIT 1""",
            (room_id, member_user_id, member_user_id),
        ).fetchone()
        if active:
            raise HTTPException(409, f"Нельзя удалить участника: сначала закрой активную задачу №{active['id']}")
        conn.execute(
            "UPDATE room_members SET left_at=CURRENT_TIMESTAMP WHERE room_id=? AND user_id=? AND left_at IS NULL",
            (room_id, member_user_id),
        )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "member_removed", "room_member", member_user_id, f"room_id={room_id}"),
        )
        return {"ok": True}


@app.put("/api/rooms/{room_id}/mode")
def put_room_mode(room_id: int, data: RoomModeIn,
                  x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            return set_room_work_mode(conn, room_id, account["id"], data.mode)
        except RoomError as error:
            room_error(error)


def _taskhub_session_secret() -> str:
    token = getenv("BOT_TOKEN", "").strip()
    if token:
        return token
    if getenv("DEV_MODE", "false").lower() == "true":
        return getenv("TASKHUB_DEV_SESSION_SECRET", "taskhub-local-development-session-secret")
    return ""


def current_user(init_data: str):
    token = getenv("BOT_TOKEN", "").strip()
    session_secret = _taskhub_session_secret()

    # A Telegram initData payload is short-lived. Exchange it once for a
    # server-signed session so an already-open Mini App does not lose API
    # access after the original Telegram auth_date ages past one hour.
    if init_data.startswith("th1."):
        telegram_id = verify_taskhub_session(init_data, session_secret)
        if not telegram_id:
            raise HTTPException(401, "Сессия TaskHub истекла. Закройте и заново откройте приложение через Telegram.")
        with connect() as conn:
            account = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not account:
                raise HTTPException(401, "Пользовательская сессия больше не действительна. Откройте TaskHub через Telegram.")
            return dict(account)

    telegram_user = verify_telegram_init_data(init_data, token)
    dev_mode = getenv("DEV_MODE", "false").lower() == "true"
    if not telegram_user and dev_mode:
        telegram_user = {"id": 999000, "first_name": "Разработчик", "username": "dev"}
    if not telegram_user:
        log.warning(
            "TELEGRAM_AUTH_REJECTED init_data_present=%s init_data_length=%s bot_token_configured=%s",
            bool(init_data), len(init_data or ""), bool(token),
        )
        raise HTTPException(
            401,
            "Telegram не подтвердил вход. Откройте TaskHub кнопкой бота; если ошибка повторяется, проверьте BOT_TOKEN в Railway.",
        )
    telegram_id = int(telegram_user["id"])
    admin_ids = {int(value.strip()) for value in getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip().isdigit()}
    moderator_ids = {int(value.strip()) for value in getenv("MODERATOR_TELEGRAM_IDS", "").split(",") if value.strip().isdigit()}
    role = "admin" if telegram_id in admin_ids or (dev_mode and telegram_id == 999000) else ("moderator" if telegram_id in moderator_ids else "helper")
    with connect() as conn:
        conn.execute(
            """INSERT INTO users(telegram_id, username, first_name, role, photo_url) VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name, role=excluded.role,
                photo_url=COALESCE(excluded.photo_url, users.photo_url)""",
            (telegram_id, telegram_user.get("username"), telegram_user.get("first_name", ""), role, telegram_user.get("photo_url")),
        )
        return dict(conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone())


def user(init_data: str):
    return current_user(init_data)


def admin(account):
    if account["role"] != "admin":
        raise HTTPException(403, "Только для администратора")

def moderator(account):
    if account["role"] not in ("admin", "moderator"):
        raise HTTPException(403, "Только для модератора")


def redact_chat_contacts(value: str) -> str:
    import re
    value=re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email скрыт]", value)
    value=re.sub(r"\+?\d[\d\s().-]{8,}\d", "[телефон скрыт]", value)
    return value

def task_base_reward(data: TaskIn) -> int:
    if data.pricing_model == "fixed":
        return int(data.reward_rub or 0) * 100
    amount = BASE_RATE_KOP_PER_HOUR * data.norm_minutes / 60 * data.difficulty * data.urgency
    return int(round(amount))


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/presence")
def update_user_presence(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        conn.execute("""INSERT INTO user_presence(user_id,last_seen) VALUES(?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",(account["id"],))
    return {"ok":True}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "TaskHub",
        "telegram_bot": _bot_status,
        "api_contract": "marketplace-v1-rooms-v1",
        "release": getenv("RAILWAY_GIT_COMMIT_SHA", "local"),
    }


@app.post("/api/session")
def create_session(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    session_token = create_taskhub_session(account["telegram_id"], _taskhub_session_secret())
    return {"token": session_token, "expires_in": 12 * 60 * 60}


@app.get("/api/me")
def me(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        migrated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'"
        ).fetchone() is not None
        if migrated:
            # Financial totals and task counts must always be requested with a room context.
            return {"user": account, "balance_kop": 0, "counts": {}}
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM tasks WHERE room_id IS NULL AND assignee_id=? GROUP BY status", (account["id"],))}
    return {"user": account, "balance_kop": balance, "counts": counts}


@app.get("/api/projects")
def projects(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*,
            COALESCE((SELECT SUM(s.budget_kop) FROM stages s WHERE s.project_id=p.id),0) stages_budget_kop,
            COALESCE((SELECT SUM(t.reward_kop) FROM tasks t WHERE t.project_id=p.id AND t.status='accepted'),0) accrued_kop
            FROM projects p ORDER BY p.id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/projects")
def create_project(data: ProjectIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data); admin(account)
    with connect() as conn:
        cur = conn.execute("INSERT INTO projects(title,description,budget_kop,created_by) VALUES(?,?,?,?)",
                           (data.title.strip(),data.description.strip(),data.budget_rub*100,account["id"]))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",
                     (account["id"],"create","project",cur.lastrowid))
        return dict(conn.execute("SELECT * FROM projects WHERE id=?",(cur.lastrowid,)).fetchone())


@app.post("/api/projects/{project_id}/stages")
def create_stage(project_id: int, data: StageIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data); admin(account)
    with connect() as conn:
        project=conn.execute("SELECT * FROM projects WHERE id=?",(project_id,)).fetchone()
        if not project: raise HTTPException(404,"Проект не найден")
        allocated=conn.execute("SELECT COALESCE(SUM(budget_kop),0) n FROM stages WHERE project_id=?",(project_id,)).fetchone()["n"]
        if allocated + data.budget_rub*100 > project["budget_kop"]:
            raise HTTPException(409,"Бюджет этапов превышает бюджет проекта")
        cur=conn.execute("INSERT INTO stages(project_id,title,description,budget_kop) VALUES(?,?,?,?)",
                         (project_id,data.title.strip(),data.description.strip(),data.budget_rub*100))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",
                     (account["id"],"create","stage",cur.lastrowid))
        return dict(conn.execute("SELECT * FROM stages WHERE id=?",(cur.lastrowid,)).fetchone())


@app.get("/api/projects/{project_id}/stages")
def project_stages(project_id:int,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM stages WHERE project_id=? ORDER BY position,id",(project_id,))]


@app.get("/api/team")
def team(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    admin(account)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT u.id,u.telegram_id,u.username,u.first_name,u.photo_url,u.role,
              COUNT(CASE WHEN t.status='in_progress' THEN 1 END) active_tasks,
              COUNT(CASE WHEN t.status='submitted' THEN 1 END) submitted_tasks,
              COALESCE((SELECT SUM(l.amount_kop) FROM ledger l WHERE l.user_id=u.id),0) earned_kop
            FROM users u LEFT JOIN tasks t ON t.assignee_id=u.id
            GROUP BY u.id ORDER BY u.created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]



@app.get("/api/categories")
def get_categories(include_hidden: bool = False, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if include_hidden:
        moderator(account)
    with connect() as conn:
        rows = conn.execute("""SELECT p.id,p.name,p.is_hidden,
            c.id child_id,c.name child_name,c.is_hidden child_hidden
            FROM categories p LEFT JOIN categories c ON c.parent_id=p.id
            WHERE p.parent_id IS NULL AND (?=1 OR p.is_hidden=0)
              AND (c.id IS NULL OR ?=1 OR c.is_hidden=0)
            ORDER BY p.id,c.id""", (int(include_hidden),int(include_hidden))).fetchall()
    result=[]
    for row in rows:
        parent=next((x for x in result if x["id"]==row["id"]),None)
        if parent is None:
            parent={"id":row["id"],"name":row["name"],"is_hidden":bool(row["is_hidden"]),"children":[]}
            result.append(parent)
        if row["child_id"] is not None:
            parent["children"].append({"id":row["child_id"],"name":row["child_name"],"is_hidden":bool(row["child_hidden"])})
    return result


@app.post("/api/categories")
def create_category(data: CategoryCreateIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    name=data.name.strip()
    with connect() as conn:
        if data.parent_id is not None:
            parent=conn.execute("SELECT id,parent_id,is_hidden FROM categories WHERE id=?", (data.parent_id,)).fetchone()
            if not parent or parent["parent_id"] is not None or parent["is_hidden"]:
                raise HTTPException(400,"Подкатегория должна принадлежать корневой категории")
        if conn.execute("SELECT 1 FROM categories WHERE name=? AND parent_id IS NULL",(name,)).fetchone() if data.parent_id is None else conn.execute("SELECT 1 FROM categories WHERE name=? AND parent_id=?",(name,data.parent_id)).fetchone():
            raise HTTPException(409,"Такая категория уже существует")
        try:
            cur=conn.execute("INSERT INTO categories(name,parent_id) VALUES(?,?)",(name,data.parent_id))
        except Exception as exc:
            if "UNIQUE" in str(exc): raise HTTPException(409,"Такая категория уже существует")
            raise
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"create_category","category",cur.lastrowid,f"name={name};parent_id={data.parent_id}"))
        return dict(conn.execute("SELECT * FROM categories WHERE id=?",(cur.lastrowid,)).fetchone())


@app.patch("/api/categories/{category_id}")
def update_category(category_id: int, data: CategoryUpdateIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        category=conn.execute("SELECT * FROM categories WHERE id=?",(category_id,)).fetchone()
        if not category: raise HTTPException(404,"Категория не найдена")
        new_parent=category["parent_id"] if data.parent_id is None else data.parent_id
        if new_parent is not None:
            parent=conn.execute("SELECT id,parent_id,is_hidden FROM categories WHERE id=?",(new_parent,)).fetchone()
            if not parent or parent["parent_id"] is not None or parent["is_hidden"] or new_parent==category_id:
                raise HTTPException(400,"Допустим только перенос под корневую категорию")
        if category["parent_id"] is None and conn.execute("SELECT 1 FROM categories WHERE parent_id=? LIMIT 1",(category_id,)).fetchone() and data.parent_id is not None:
            raise HTTPException(409,"Сначала перенеси или скрой подкатегории")
        name=(data.name.strip() if data.name is not None else category["name"])
        hidden=(int(data.is_hidden) if data.is_hidden is not None else category["is_hidden"])
        try:
            conn.execute("UPDATE categories SET name=?,parent_id=?,is_hidden=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(name,new_parent,hidden,category_id))
        except Exception as exc:
            if "UNIQUE" in str(exc): raise HTTPException(409,"В этой ветке уже есть такая категория")
            raise
        if new_parent is None:
            children=conn.execute("SELECT id,name FROM categories WHERE parent_id=?",(category_id,)).fetchall()
            for child in children:
                conn.execute("UPDATE tasks SET category=? WHERE category_id=?",(name+" / "+child["name"],child["id"]))
        else:
            parent_name=conn.execute("SELECT name FROM categories WHERE id=?",(new_parent,)).fetchone()["name"]
            conn.execute("UPDATE tasks SET category=? WHERE category_id=?",(parent_name+" / "+name,category_id))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"update_category","category",category_id,f"name={name};parent_id={new_parent};is_hidden={hidden}"))
        return dict(conn.execute("SELECT * FROM categories WHERE id=?",(category_id,)).fetchone())


@app.get("/api/category-requests")
def list_category_requests(status: str = "pending", x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        rows=conn.execute("""SELECT r.*,u.first_name requester_name,t.title task_title,
            c.name resolved_category FROM category_requests r JOIN users u ON u.id=r.user_id
            JOIN tasks t ON t.id=r.task_id LEFT JOIN categories c ON c.id=r.resolved_category_id
            WHERE r.status=? ORDER BY r.created_at""",(status,)).fetchall()
    return [dict(x) for x in rows]


@app.post("/api/category-requests/{request_id}/resolve")
async def resolve_category_request(request_id: int, data: CategoryRequestResolveIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        row=conn.execute("SELECT r.*,u.telegram_id,t.title FROM category_requests r JOIN users u ON u.id=r.user_id JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND r.status='pending'",(request_id,)).fetchone()
        if not row: raise HTTPException(404,"Заявка не найдена")
        if data.status in ("added","moved"):
            if not data.category_id: raise HTTPException(400,"Выбери итоговую категорию")
            cat=conn.execute("SELECT c.*,p.name parent_name FROM categories c LEFT JOIN categories p ON p.id=c.parent_id WHERE c.id=? AND c.parent_id IS NOT NULL",(data.category_id,)).fetchone()
            if not cat or cat["is_hidden"]: raise HTTPException(400,"Выбери видимую подкатегорию")
            conn.execute("UPDATE tasks SET category_id=?,category=? WHERE id=?",(cat["id"],cat["parent_name"]+" / "+cat["name"],row["task_id"]))
        conn.execute("UPDATE category_requests SET status=?,resolved_category_id=?,moderator_id=?,resolution_note=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                     (data.status,data.category_id,account["id"],data.note.strip(),request_id))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"resolve_category_request","category_request",request_id,f"{data.status};category_id={data.category_id}"))
    await notify_telegram(row["telegram_id"],f"📂 Решение по твоей заявке на категорию для задания «{row['title']}»: {data.status}. {data.note}".strip())
    return {"ok":True,"status":data.status}



@app.get("/api/onboarding")
def get_onboarding(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        row=conn.execute(
            "SELECT role_flags,onboarding_step,onboarding_completed_at,onboarding_skipped_at FROM users WHERE id=?",
            (account["id"],)
        ).fetchone()
    return {
        "role_flags": row["role_flags"] or "none",
        "step": row["onboarding_step"] or 0,
        "completed": bool(row["onboarding_completed_at"]),
        "skipped": bool(row["onboarding_skipped_at"]),
    }


@app.put("/api/onboarding")
def update_onboarding(data: OnboardingUpdateIn,
                      x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    updates=[]
    values=[]
    if data.role_flags is not None:
        updates.extend(["role_flags=?","onboarding_step=2","onboarding_skipped_at=NULL"])
        values.append(data.role_flags)
    if data.completed is True:
        updates.extend(["onboarding_completed_at=CURRENT_TIMESTAMP","onboarding_step=0","onboarding_skipped_at=NULL"])
    if data.skipped is True:
        updates.extend(["onboarding_skipped_at=CURRENT_TIMESTAMP","onboarding_step=0"])
    if updates:
        with connect() as conn:
            conn.execute("UPDATE users SET "+",".join(updates)+" WHERE id=?",(*values,account["id"]))
            conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                         (account["id"],"update_onboarding","user",account["id"],
                          "role_flags="+str(data.role_flags or "")+";completed="+str(data.completed)+";skipped="+str(data.skipped)))
    return {"ok":True}


@app.get("/api/market/subscriptions")
def get_market_subscriptions(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        rows=conn.execute("""SELECT c.id,c.name,c.parent_id,p.name parent_name FROM subscriptions s
            JOIN categories c ON c.id=s.category_id LEFT JOIN categories p ON p.id=c.parent_id
            WHERE s.user_id=? ORDER BY c.parent_id,c.id""",(account["id"],)).fetchall()
    return [dict(x) for x in rows]


@app.put("/api/market/subscriptions")
def set_market_subscriptions(data: SubscriptionUpdateIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    ids=list(dict.fromkeys(data.category_ids))
    if len(ids)>20: raise HTTPException(400,"Можно выбрать не более 20 подписок")
    with connect() as conn:
        if ids:
            placeholders=",".join("?" for _ in ids)
            rows=conn.execute(f"""SELECT c.id,c.is_hidden,COALESCE(p.is_hidden,0) parent_hidden
                FROM categories c LEFT JOIN categories p ON p.id=c.parent_id WHERE c.id IN ({placeholders})""",ids).fetchall()
            if len(rows)!=len(ids) or any(x["is_hidden"] or x["parent_hidden"] for x in rows):
                raise HTTPException(400,"В подписку можно добавить только существующие видимые категории")
        conn.execute("DELETE FROM subscriptions WHERE user_id=?",(account["id"],))
        conn.executemany("INSERT INTO subscriptions(user_id,category_id) VALUES(?,?)",[(account["id"],x) for x in ids])
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"update_subscriptions","user",account["id"],"category_ids="+",".join(map(str,ids))))
    return {"ok":True,"category_ids":ids}


@app.delete("/api/market/subscriptions/{category_id}")
def delete_market_subscription(category_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        conn.execute("DELETE FROM subscriptions WHERE user_id=? AND category_id=?",(account["id"],category_id))
    return {"ok":True}


@app.get("/api/market/profile")
def market_profile(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,COUNT(*) n FROM tasks WHERE room_id IS NULL AND assignee_id=? GROUP BY status", (account["id"],)
        )}
        created = conn.execute("SELECT COUNT(*) n FROM tasks WHERE room_id IS NULL AND created_by=?", (account["id"],)).fetchone()["n"]
        reputation = conn.execute("SELECT ROUND(AVG(rating),1) average,COUNT(*) review_count FROM reviews WHERE target_id=?", (account["id"],)).fetchone()
        achievements = [dict(x) for x in conn.execute("SELECT code,earned_at FROM achievements WHERE user_id=? ORDER BY earned_at",(account["id"],)).fetchall()]
        mine = conn.execute(
            "SELECT tasks.*,(SELECT requested_by FROM task_cancel_requests cr WHERE cr.task_id=tasks.id AND cr.status='pending') cancel_requested_by FROM tasks WHERE room_id IS NULL AND (created_by=? OR assignee_id=?) ORDER BY id DESC LIMIT 100",
            (account["id"], account["id"]),
        ).fetchall()
    return {"user": account, "balance_kop": balance, "counts": counts, "created_count": created, "rating_average": reputation["average"] or 0, "review_count": reputation["review_count"], "achievements": achievements, "tasks": [dict(r) for r in mine]}


def _provider_json(url: str, user_agent: str = "TaskHub/1.0 (https://taskhub-production-87811e29.up.railway.app)"):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Delivery provider request failed: %s", type(exc).__name__)
        raise HTTPException(503, "Сервис маршрутов или погоды временно недоступен")


def _geocode_delivery_address(city: str, address: str):
    query = urllib.parse.urlencode({"q": f"{address}, {city}", "format": "jsonv2", "limit": 1})
    rows = _provider_json("https://nominatim.openstreetmap.org/search?" + query)
    if not rows:
        raise HTTPException(422, f"Не удалось найти адрес: {address}")
    return float(rows[0]["lon"]), float(rows[0]["lat"])


def _delivery_weather(lat: float, lon: float, route_duration_seconds: float = 0):
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m",
        "hourly": "temperature_2m,precipitation_probability,rain,snowfall,weather_code",
        "forecast_days": 2, "timezone": "auto",
    })
    payload = _provider_json("https://api.open-meteo.com/v1/forecast?" + query)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return "clear"
    current = datetime.fromisoformat((payload.get("current") or {}).get("time", times[0]))
    target = current + timedelta(seconds=max(0, route_duration_seconds) / 2)
    index = min(range(len(times)), key=lambda i: abs((datetime.fromisoformat(times[i]) - target).total_seconds()))
    code = int((hourly.get("weather_code") or [0])[index] or 0)
    temp = float((hourly.get("temperature_2m") or [10])[index] or 10)
    rain = float((hourly.get("rain") or [0])[index] or 0)
    snow = float((hourly.get("snowfall") or [0])[index] or 0)
    probability = int((hourly.get("precipitation_probability") or [0])[index] or 0)
    if code in (56, 57, 66, 67):
        return "ice"
    if temp <= -15:
        return "strong_frost"
    if code in (71, 73, 75, 77, 85, 86) or snow > 0:
        return "snow"
    if code in (65, 82) or rain >= 7.5 or probability >= 80 and rain > 0:
        return "heavy_rain"
    if code in (51, 53, 55, 61, 63, 80, 81) or rain > 0:
        return "rain"
    return "clear"


@app.post("/api/market/tasks/{task_id}/delivery-quote")
def calculate_market_delivery_quote(task_id: int, data: DeliveryQuoteIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute("""SELECT t.*,r.executor_id FROM tasks t
            LEFT JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            WHERE t.id=? AND t.room_id IS NULL""", (task_id,)).fetchone()
        if not task or task["status"] not in ("open", "available", "in_progress"):
            raise HTTPException(403, "Расчёт маршрута недоступен для этого задания")
        if task["status"] == "in_progress" and int(task["executor_id"] or -1) != int(account["id"]):
            raise HTTPException(403, "Пересчитать тариф может только выбранный исполнитель")
        if task["task_mode"] != "irl" or not task["pickup_address"] or not task["delivery_address"]:
            raise HTTPException(409, "Для доставки нужны адрес забора и адрес назначения")
        if task["status"] == "in_progress" and task["delivery_courier_pay_kop"] is not None:
            return {"distance_km": task["delivery_distance_km"], "weather": task["delivery_weather"],
                    "weather_factor": task["delivery_weather_factor"], "courier_pay_rub": round(task["delivery_courier_pay_kop"] / 100),
                    "locked": True}
        city = task["city"]
        pickup_lon, pickup_lat = _geocode_delivery_address(city, task["pickup_address"])
        delivery_lon, delivery_lat = _geocode_delivery_address(city, task["delivery_address"])
        coordinates = f"{data.start_lon},{data.start_lat};{pickup_lon},{pickup_lat};{delivery_lon},{delivery_lat}"
        route = _provider_json("https://router.project-osrm.org/route/v1/driving/" + coordinates + "?overview=false&steps=false")
        routes = route.get("routes") or []
        if not routes:
            raise HTTPException(422, "Не удалось построить автомобильный маршрут")
        distance_km = float(routes[0]["distance"]) / 1000
        weather = _delivery_weather((data.start_lat + pickup_lat + delivery_lat) / 3, (data.start_lon + pickup_lon + delivery_lon) / 3, float(routes[0].get('duration') or 0))
        from .delivery_pricing import calculate_delivery_quote
        quote = calculate_delivery_quote(distance_km, weather)
        pay_kop = quote.total_rub * 100
        if task["status"] == "in_progress":
            conn.execute("""UPDATE tasks SET pickup_lat=?,pickup_lon=?,delivery_lat=?,delivery_lon=?,
                delivery_distance_km=?,delivery_weather=?,delivery_weather_factor=?,delivery_courier_pay_kop=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND delivery_courier_pay_kop IS NULL""",
                (pickup_lat, pickup_lon, delivery_lat, delivery_lon, quote.distance_km, weather,
                 quote.weather_factor, pay_kop, task_id))
        return {"distance_km": quote.distance_km, "weather": weather, "weather_factor": quote.weather_factor,
                "base_rub": quote.base_rub, "distance_rub": quote.distance_rub,
                "courier_pay_rub": quote.total_rub, "locked": task["status"] == "in_progress"}


@app.post("/api/market/tasks/{task_id}/attachments")
def upload_market_task_attachment(task_id: int, data: TaskAttachmentIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    try:
        raw = base64.b64decode(data.data_base64, validate=True)
    except Exception:
        raise HTTPException(400, "Некорректный файл изображения")
    if not raw or len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Изображение должно быть не больше 10 МБ")
    signatures = {
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not signatures.get(data.mime_type, False):
        raise HTTPException(415, "Содержимое файла не соответствует формату изображения")
    with connect() as conn:
        task = conn.execute("SELECT id,created_by,room_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task["room_id"] is not None or int(task["created_by"]) != int(account["id"]):
            raise HTTPException(404, "Задание не найдено")
        count = conn.execute("SELECT COUNT(*) n FROM task_attachments WHERE task_id=?", (task_id,)).fetchone()["n"]
        if count >= 5:
            raise HTTPException(409, "К одному заданию можно прикрепить не больше 5 фотографий")
        upload_dir = Path(getenv("TASKHUB_UPLOAD_DIR", "/data/taskhub_uploads"))
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            probe = upload_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            upload_dir = ROOT / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[data.mime_type]
        filename = secrets.token_hex(16) + extension
        target = upload_dir / filename
        target.write_bytes(raw)
        cur = conn.execute(
            "INSERT INTO task_attachments(task_id,uploader_id,file_path,mime_type,file_size) VALUES(?,?,?,?,?)",
            (task_id, account["id"], str(target), data.mime_type, len(raw)),
        )
        return {"id": cur.lastrowid, "mime_type": data.mime_type, "file_size": len(raw)}


@app.get("/api/market/tasks/{task_id}/attachments")
def list_market_task_attachments(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute("""SELECT t.created_by,t.status,r.executor_id FROM tasks t
            LEFT JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            WHERE t.id=? AND t.room_id IS NULL""", (task_id,)).fetchone()
        if not task or (int(account["id"]) not in (int(task["created_by"]), int(task["executor_id"] or -1)) and task["status"] not in ("open", "available")):
            raise HTTPException(404, "Вложения не найдены")
        rows = conn.execute("SELECT id,mime_type,file_size FROM task_attachments WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [{"id": row["id"], "mime_type": row["mime_type"], "file_size": row["file_size"],
                 "url": f"/api/market/tasks/{task_id}/attachments/{row['id']}"} for row in rows]


@app.get("/api/market/tasks/{task_id}/attachments/{attachment_id}")
def get_market_task_attachment(task_id: int, attachment_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        row = conn.execute("""SELECT a.file_path,a.mime_type,t.created_by,t.status,r.executor_id
            FROM task_attachments a JOIN tasks t ON t.id=a.task_id
            LEFT JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            WHERE a.id=? AND a.task_id=? AND t.room_id IS NULL""", (attachment_id, task_id)).fetchone()
        if not row or (int(account["id"]) not in (int(row["created_by"]), int(row["executor_id"] or -1)) and row["status"] not in ("open", "available")):
            raise HTTPException(404, "Фотография не найдена")
        path = Path(row["file_path"])
        if not path.is_file():
            raise HTTPException(404, "Фотография больше недоступна")
        return FileResponse(path, media_type=row["mime_type"], filename=path.name)


@app.get("/api/market/courier-profile")
def get_courier_profile(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        row = conn.execute("SELECT * FROM courier_profiles WHERE user_id=?", (account["id"],)).fetchone()
    return dict(row) if row else None


@app.put("/api/market/courier-profile")
def save_courier_profile(data: CourierProfileIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        conn.execute("""INSERT INTO courier_profiles(
            user_id,city,transport,max_weight_kg,max_length_cm,max_width_cm,max_height_cm,max_items)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET city=excluded.city,transport=excluded.transport,
            max_weight_kg=excluded.max_weight_kg,max_length_cm=excluded.max_length_cm,
            max_width_cm=excluded.max_width_cm,max_height_cm=excluded.max_height_cm,
            max_items=excluded.max_items,updated_at=CURRENT_TIMESTAMP""",
            (account["id"],data.city.strip(),data.transport,data.max_weight_kg,
             data.max_length_cm,data.max_width_cm,data.max_height_cm,data.max_items))
        return dict(conn.execute("SELECT * FROM courier_profiles WHERE user_id=?", (account["id"],)).fetchone())


@app.get("/api/market/ledger")
def market_ledger(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        rows = conn.execute(
            "SELECT l.*,t.title FROM ledger l LEFT JOIN tasks t ON t.id=l.task_id WHERE l.user_id=? ORDER BY l.id DESC",
            (account["id"],),
        ).fetchall()
    return {"balance_kop": balance, "entries": [dict(r) for r in rows]}


@app.get("/api/market/tasks")
def market_tasks(status: str = "available", limit: int = 50, offset: int = 0, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    limit=max(1,min(limit,100));offset=max(0,offset)
    with connect() as conn:
        if status == "mine":
            rows = conn.execute(
                """SELECT tasks.*,(SELECT requested_by FROM task_cancel_requests cr WHERE cr.task_id=tasks.id AND cr.status='pending') cancel_requested_by
                FROM tasks WHERE room_id IS NULL AND (created_by=? OR assignee_id=?) ORDER BY id DESC LIMIT ? OFFSET ?""",
                (account["id"],account["id"],limit,offset),
            ).fetchall()
        elif status == "available":
            rows = conn.execute(
                """SELECT t.*,(SELECT requested_by FROM task_cancel_requests cr WHERE cr.task_id=t.id AND cr.status='pending') cancel_requested_by,
                u.first_name creator_name FROM tasks t LEFT JOIN users u ON u.id=t.created_by
                WHERE t.room_id IS NULL AND t.status IN ('open','available') AND t.moderation_status='approved'
                ORDER BY t.id DESC LIMIT ? OFFSET ?""",(limit,offset)
            ).fetchall()
        else:
            raise HTTPException(400, "Неизвестный фильтр биржи")
    return [dict(row) for row in rows]


@app.post("/api/market/tasks")
async def create_market_task(data: TaskIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if not data.title.strip():
        raise HTTPException(422, "Название задания обязательно")
    if not data.category_id and not data.category.strip():
        raise HTTPException(422, "Выбери категорию из списка")
    if data.project_id or data.stage_id or data.tour_id:
        raise HTTPException(400, "Публичные задания биржи не привязываются к проектам комнаты")
    reward = task_base_reward(data)
    with connect() as conn:
        category_label=data.category.strip()
        category_id=None
        if data.category_id:
            category=conn.execute("""SELECT c.*,p.name parent_name FROM categories c
                JOIN categories p ON p.id=c.parent_id WHERE c.id=? AND c.is_hidden=0 AND p.is_hidden=0""",(data.category_id,)).fetchone()
            if not category:
                raise HTTPException(400,"Выбери доступную подкатегорию")
            category_id=category["id"]
            category_label=category["parent_name"]+" / "+category["name"]
        else:
            raise HTTPException(400,"Необходимо выбрать подкатегорию из дерева категорий")
        if category_label=="Другое / Прочее" and not (data.other_category_text or "").strip():
            raise HTTPException(422,"Опиши, какая услуга нужна (до 200 символов)")
        cur = conn.execute(
            """INSERT INTO tasks(title,description,category,category_id,reward_kop,created_by,author_id,deadline,
               pricing_model,norm_minutes,difficulty,urgency,fixed_reward_kop,budget_reserved_kop,
               status,moderation_status,room_id,task_mode,city,pickup_address,delivery_address,
               pickup_lat,pickup_lon,delivery_lat,delivery_lon,parcel_weight_kg,parcel_length_cm,
               parcel_width_cm,parcel_height_cm,parcel_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.title.strip(), data.description.strip(), category_label, category_id, reward,
             account["id"], account["id"], data.deadline, data.pricing_model, data.norm_minutes,
             data.difficulty, data.urgency, reward, 0, "open", "approved", data.task_mode,
             data.city.strip(), data.pickup_address.strip(), data.delivery_address.strip(),
             data.pickup_lat, data.pickup_lon, data.delivery_lat, data.delivery_lon,
             data.parcel_weight_kg, data.parcel_length_cm, data.parcel_width_cm, data.parcel_height_cm, data.parcel_count),
        )
        task_id = cur.lastrowid
        conn.execute("INSERT OR IGNORE INTO achievements(user_id,code) VALUES(?,'first_task')",(account["id"],))
        if category_label=="Другое / Прочее":
            conn.execute("INSERT INTO category_requests(user_id,task_id,text) VALUES(?,?,?)",
                         (account["id"],task_id,(data.other_category_text or "").strip()))
        for pos, item in enumerate(data.criteria):
            conn.execute(
                "INSERT INTO task_criteria(task_id,title,weight,critical,position) VALUES(?,?,?,?,?)",
                (task_id, item.title.strip(), item.weight, int(item.critical), pos),
            )
        conn.execute("UPDATE tasks SET criteria_frozen=1 WHERE id=?", (task_id,))
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "publish_market_task", "task", task_id, "public_marketplace"),
        )
        task_row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    return task_row


@app.put("/api/market/tasks/{task_id}")
async def update_market_task(task_id: int, data: MarketTaskUpdate, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.project_id or data.stage_id or data.tour_id:
        raise HTTPException(400, "Публичные задания не привязываются к проектам комнаты")
    reward = task_base_reward(data)
    notify_executor = None
    with connect() as conn:
        task = conn.execute("""SELECT t.*,r.executor_id,u.telegram_id executor_telegram_id
            FROM tasks t LEFT JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            LEFT JOIN users u ON u.id=r.executor_id
            WHERE t.id=? AND t.room_id IS NULL AND t.created_by=?""", (task_id, account["id"])).fetchone()
        if not task:
            raise HTTPException(404, "Твоё задание не найдено")
        if task["status"] not in ("open","available","in_progress"):
            raise HTTPException(409, "Это задание больше нельзя редактировать")
        active_deal = task["status"] == "in_progress"
        if active_deal and data.title.strip() != task["title"]:
            raise HTTPException(409, "После выбора исполнителя можно менять только описание и срок")
        if active_deal and task["deadline"] and (not data.deadline or data.deadline[:10] < task["deadline"][:10]):
            raise HTTPException(409, "После выбора исполнителя срок можно только продлить")
        if active_deal and (reward != task["reward_kop"] or (data.category_id and data.category_id != task["category_id"]) or data.category != task["category"]):
            raise HTTPException(409, "После выбора исполнителя награду и категорию менять нельзя")
        category_label=task["category"]; category_id=task["category_id"]
        if not active_deal and data.category_id:
            category=conn.execute("""SELECT c.*,p.name parent_name FROM categories c JOIN categories p ON p.id=c.parent_id
                WHERE c.id=? AND c.is_hidden=0 AND p.is_hidden=0""",(data.category_id,)).fetchone()
            if not category: raise HTTPException(400,"Выбери доступную подкатегорию")
            category_id=category["id"];category_label=category["parent_name"]+" / "+category["name"]
        values={"title":data.title.strip(),"description":data.description.strip(),"category":category_label,
                "category_id":category_id,"reward_kop":task["reward_kop"] if active_deal else reward,
                "deadline":data.deadline,"pricing_model":task["pricing_model"] if active_deal else data.pricing_model,
                "norm_minutes":task["norm_minutes"] if active_deal else data.norm_minutes,
                "difficulty":task["difficulty"] if active_deal else data.difficulty,
                "urgency":task["urgency"] if active_deal else data.urgency,
                "fixed_reward_kop":task["fixed_reward_kop"] if active_deal else reward,
                "task_mode":task["task_mode"] if active_deal else data.task_mode,
                "city":task["city"] if active_deal else data.city.strip(),
                "pickup_address":task["pickup_address"] if active_deal else data.pickup_address.strip(),
                "delivery_address":task["delivery_address"] if active_deal else data.delivery_address.strip(),
                "parcel_weight_kg":task["parcel_weight_kg"] if active_deal else data.parcel_weight_kg,
                "parcel_length_cm":task["parcel_length_cm"] if active_deal else data.parcel_length_cm,
                "parcel_width_cm":task["parcel_width_cm"] if active_deal else data.parcel_width_cm,
                "parcel_height_cm":task["parcel_height_cm"] if active_deal else data.parcel_height_cm,
                "parcel_count":task["parcel_count"] if active_deal else data.parcel_count}
        for field,new_value in values.items():
            old_value=task[field]
            if str(old_value or "")!=str(new_value or ""):
                conn.execute("INSERT INTO task_history(task_id,changed_by,field,old_value,new_value) VALUES(?,?,?,?,?)",
                             (task_id,account["id"],field,str(old_value) if old_value is not None else None,str(new_value) if new_value is not None else None))
        conn.execute("""UPDATE tasks SET title=?,description=?,category=?,category_id=?,reward_kop=?,deadline=?,
            pricing_model=?,norm_minutes=?,difficulty=?,urgency=?,fixed_reward_kop=?,task_mode=?,city=?,
            pickup_address=?,delivery_address=?,parcel_weight_kg=?,parcel_length_cm=?,parcel_width_cm=?,
            parcel_height_cm=?,parcel_count=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND created_by=? AND room_id IS NULL""",
            (values["title"],values["description"],values["category"],values["category_id"],values["reward_kop"],values["deadline"],
             values["pricing_model"],values["norm_minutes"],values["difficulty"],values["urgency"],
             values["fixed_reward_kop"],values["task_mode"],values["city"],values["pickup_address"],values["delivery_address"],
             values["parcel_weight_kg"],values["parcel_length_cm"],values["parcel_width_cm"],values["parcel_height_cm"],values["parcel_count"],task_id,account["id"]))
        if active_deal:
            notify_executor=task["executor_telegram_id"]
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"edit_market_task","task",task_id,"active_deal="+str(active_deal)))
        updated=dict(conn.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone())
    if notify_executor:
        await notify_telegram(notify_executor,f"✏️ Заказчик изменил описание или срок задания «{updated['title']}». Ты можешь отказаться от сделки без штрафа.")
    return updated


@app.delete("/api/market/tasks/{task_id}")
async def delete_market_task(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND room_id IS NULL AND created_by=?", (task_id, account["id"])).fetchone()
        if not task:
            raise HTTPException(404, "Твоё задание не найдено")
        if task["status"] not in ("open", "available") or task["assignee_id"] is not None:
            raise HTTPException(409, "Нельзя отменить задание, которое уже взяли в работу")
        recipients=conn.execute("SELECT u.telegram_id FROM responses r JOIN users u ON u.id=r.executor_id WHERE r.task_id=? AND r.status='sent'",(task_id,)).fetchall()
        conn.execute("UPDATE tasks SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND created_by=? AND status IN ('open','available')", (task_id, account["id"]))
        conn.execute("UPDATE responses SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND status='sent'",(task_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "cancel_market_task", "task", task_id, "customer_cancelled_open_task"))
    for recipient in recipients:
        await notify_telegram(recipient["telegram_id"],f"Заказчик отменил задание «{task['title']}». Отклик закрыт.")
    return {"ok": True, "status":"cancelled"}


@app.get("/api/market/notification-preferences")
def get_market_notification_preferences(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    import json
    account = user(x_telegram_init_data)
    with connect() as conn:
        row = conn.execute("SELECT categories,enabled FROM market_notification_preferences WHERE user_id=?", (account["id"],)).fetchone()
    return {"categories": json.loads(row["categories"]) if row else [], "enabled": bool(row["enabled"]) if row else False}


@app.put("/api/market/notification-preferences")
def set_market_notification_preferences(data: MarketNotificationPrefs, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    import json
    account = user(x_telegram_init_data)
    categories = list(dict.fromkeys(x.strip() for x in data.categories if x.strip()))
    with connect() as conn:
        conn.execute("""INSERT INTO market_notification_preferences(user_id,categories,enabled,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET categories=excluded.categories,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
            (account["id"], json.dumps(categories, ensure_ascii=False), int(data.enabled)))
    return {"ok": True, "categories": categories, "enabled": data.enabled}


@app.get("/api/market/services")
def market_services(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410,"Каталог постоянных услуг не входит в MVP TaskHub.")


@app.post("/api/market/services")
def create_market_service(data: MarketServiceIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410,"Публикация постоянных услуг не входит в MVP TaskHub.")


@app.post("/api/market/services/{service_id}/orders")
def create_market_service_order(service_id: int, data: MarketServiceOrderIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410,"Заказы постоянных услуг не входят в MVP TaskHub.")


@app.get("/api/market/service-orders")
def market_service_orders(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410,"Заказы постоянных услуг не входят в MVP TaskHub.")


@app.patch("/api/market/service-orders/{order_id}")
def update_market_service_order(order_id: int, data: MarketServiceOrderStatusIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410,"Заказы постоянных услуг не входят в MVP TaskHub.")


@app.get("/api/market/tasks/{task_id}/responses")
def list_market_responses(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("SELECT * FROM tasks WHERE id=? AND room_id IS NULL",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Задание не найдено")
        if int(task["created_by"])==int(account["id"]):
            rows=conn.execute("SELECT r.*,u.first_name executor_name,u.username executor_username FROM responses r JOIN users u ON u.id=r.executor_id WHERE r.task_id=? ORDER BY r.created_at",(task_id,)).fetchall()
        else:
            rows=conn.execute("SELECT r.* FROM responses r WHERE r.task_id=? AND r.executor_id=?",(task_id,account["id"])).fetchall()
    return [dict(x) for x in rows]


@app.post("/api/market/tasks/{task_id}/responses")
async def create_market_response(task_id: int, data: MarketResponseIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("SELECT t.*,u.telegram_id owner_telegram_id FROM tasks t JOIN users u ON u.id=t.created_by WHERE t.id=? AND t.room_id IS NULL AND t.status='open' AND t.moderation_status='approved'",(task_id,)).fetchone()
        if not task: raise HTTPException(409,"Задание уже недоступно для откликов")
        if int(task["created_by"])==int(account["id"]): raise HTTPException(403,"Нельзя откликнуться на своё задание")
        try:
            cur=conn.execute("INSERT INTO responses(task_id,executor_id,message,proposed_deadline) VALUES(?,?,?,?)",(task_id,account["id"],data.message.strip(),data.proposed_deadline))
        except Exception as exc:
            if "UNIQUE" in str(exc): raise HTTPException(409,"Ты уже откликался на это задание")
            raise
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"create_response","response",cur.lastrowid,f"task_id={task_id}"))
        conn.execute("INSERT OR IGNORE INTO achievements(user_id,code) VALUES(?,'first_response')",(account["id"],))
    await notify_telegram(task["owner_telegram_id"],f"✉️ Новый отклик на задание «{task['title']}»")
    return {"id":cur.lastrowid,"status":"sent"}


@app.post("/api/market/responses/{response_id}/withdraw")
def withdraw_market_response(response_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        row=conn.execute("SELECT * FROM responses WHERE id=? AND executor_id=? AND status='sent'",(response_id,account["id"])).fetchone()
        if not row: raise HTTPException(404,"Отклик нельзя отозвать")
        conn.execute("UPDATE responses SET status='withdrawn',updated_at=CURRENT_TIMESTAMP WHERE id=?",(response_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(account["id"],"withdraw_response","response",response_id))
    return {"ok":True,"status":"withdrawn"}


@app.post("/api/market/responses/{response_id}/accept")
async def accept_market_response(response_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    rejected_recipients=[]
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("""SELECT r.*,t.id task_id,t.title,t.created_by,t.status task_status,
            u.telegram_id executor_telegram_id FROM responses r JOIN tasks t ON t.id=r.task_id
            JOIN users u ON u.id=r.executor_id WHERE r.id=? AND t.room_id IS NULL""",(response_id,)).fetchone()
        if not row or int(row["created_by"])!=int(account["id"]): raise HTTPException(404,"Отклик не найден")
        if row["task_status"]!="open" or row["status"]!="sent": raise HTTPException(409,"Задание или отклик уже обработаны")
        others=conn.execute("""SELECT u.telegram_id FROM responses r JOIN users u ON u.id=r.executor_id
            WHERE r.task_id=? AND r.id<>? AND r.status='sent'""",(row["task_id"],response_id)).fetchall()
        rejected_recipients=[x["telegram_id"] for x in others if x["telegram_id"]]
        cur=conn.execute("UPDATE tasks SET status='in_progress',assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'",(row["executor_id"],row["task_id"]))
        if cur.rowcount!=1: raise HTTPException(409,"Задание уже выбрал другой заказчик или оно закрыто")
        conn.execute("UPDATE responses SET status='accepted',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='sent'",(response_id,))
        conn.execute("UPDATE responses SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND id<>? AND status='sent'",(row["task_id"],response_id))
        cur=conn.execute("INSERT INTO chats(response_id) VALUES(?)",(response_id,))
        chat_id=cur.lastrowid
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"accept_response","response",response_id,f"chat_id={chat_id}"))
    await notify_telegram(row["executor_telegram_id"],f"✅ Твой отклик принят: «{row['title']}». Чат с заказчиком открыт.")
    for recipient in rejected_recipients:
        await notify_telegram(recipient,f"Заказчик выбрал другого исполнителя для задания «{row['title']}». Спасибо за отклик.")
    return {"ok":True,"chat_id":chat_id,"status":"in_progress"}


@app.get("/api/market/responses/mine")
def my_market_responses(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        rows=conn.execute("""SELECT r.*,t.title,t.category,t.reward_kop,t.status task_status,t.created_by,
            owner.first_name customer_name FROM responses r JOIN tasks t ON t.id=r.task_id
            JOIN users owner ON owner.id=t.created_by WHERE r.executor_id=? ORDER BY r.created_at DESC""",(account["id"],)).fetchall()
    return [dict(x) for x in rows]


@app.post("/api/market/responses/{response_id}/release")
async def release_accepted_market_response(response_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        row=conn.execute("""SELECT r.*,t.id task_id,t.title,t.created_by,t.status task_status,
            owner.telegram_id owner_telegram_id FROM responses r JOIN tasks t ON t.id=r.task_id
            JOIN users owner ON owner.id=t.created_by WHERE r.id=? AND r.executor_id=?""",(response_id,account["id"])).fetchone()
        if not row or row["status"]!="accepted" or row["task_status"]!="in_progress":
            raise HTTPException(409,"Эту сделку нельзя отменить со стороны исполнителя")
        conn.execute("UPDATE responses SET status='withdrawn',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='accepted'",(response_id,))
        conn.execute("UPDATE tasks SET status='open',assignee_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='in_progress'",(row["task_id"],))
        conn.execute("UPDATE task_cancel_requests SET status='rejected',resolved_at=CURRENT_TIMESTAMP WHERE task_id=? AND status='pending'",(row["task_id"],))
        conn.execute("UPDATE chats SET closed_at=CURRENT_TIMESTAMP WHERE response_id=?",(response_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"executor_release_deal","task",row["task_id"],"withdraw_without_penalty"))
    await notify_telegram(row["owner_telegram_id"],f"Исполнитель отказался от задания «{row['title']}». Задание снова открыто для откликов.")
    return {"ok":True,"status":"open"}


@app.post("/api/market/messages/{message_id}/hide")
def hide_market_message(message_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        row=conn.execute("""SELECT m.id,m.sender_id,t.created_by,r.executor_id FROM messages m
            JOIN chats c ON c.id=m.chat_id JOIN responses r ON r.id=c.response_id
            JOIN tasks t ON t.id=r.task_id WHERE m.id=?""",(message_id,)).fetchone()
        if not row or int(row["sender_id"])!=int(account["id"]):
            raise HTTPException(403,"Скрыть сообщение для собеседника может только его автор")
        recipient_id=row["executor_id"] if int(row["sender_id"])==int(row["created_by"]) else row["created_by"]
        conn.execute("INSERT OR IGNORE INTO message_hides(message_id,user_id) VALUES(?,?)",(message_id,recipient_id))
    return {"ok":True,"hidden_for_user_id":recipient_id}




@app.get("/api/moderation/disputes")
def moderator_disputes(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        rows=conn.execute("""SELECT d.*,t.title,t.status task_status,
            opener.first_name opened_by_name,customer.first_name customer_name,executor.first_name executor_name
            FROM disputes d JOIN tasks t ON t.id=d.task_id JOIN users opener ON opener.id=d.user_id
            JOIN users customer ON customer.id=t.created_by
            LEFT JOIN responses r ON r.task_id=t.id AND r.status IN ('accepted','withdrawn')
            LEFT JOIN users executor ON executor.id=r.executor_id
            WHERE d.status='open' ORDER BY d.created_at""").fetchall()
    return [dict(x) for x in rows]


@app.post("/api/moderation/disputes/{dispute_id}/resolve")
async def resolve_market_dispute(dispute_id: int, data: DisputeResolutionIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        row=conn.execute("""SELECT d.*,t.title,t.status task_status,t.created_by,
            owner.telegram_id owner_telegram_id,r.executor_id,executor.telegram_id executor_telegram_id
            FROM disputes d JOIN tasks t ON t.id=d.task_id JOIN users owner ON owner.id=t.created_by
            LEFT JOIN responses r ON r.task_id=t.id AND r.status IN ('accepted','withdrawn')
            LEFT JOIN users executor ON executor.id=r.executor_id
            WHERE d.id=? AND d.status='open'""",(dispute_id,)).fetchone()
        if not row: raise HTTPException(404,"Открытая жалоба не найдена")
        if data.decision=="done":
            task_status="done"
            conn.execute("UPDATE tasks SET status='done',completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE id=?",(row["task_id"],))
            conn.execute("UPDATE chats SET closed_at=COALESCE(closed_at,CURRENT_TIMESTAMP) WHERE response_id IN (SELECT id FROM responses WHERE task_id=?)",(row["task_id"],))
        elif data.decision=="cancelled":
            task_status="cancelled"
            conn.execute("UPDATE tasks SET status='cancelled',assignee_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(row["task_id"],))
            conn.execute("UPDATE chats SET closed_at=CURRENT_TIMESTAMP WHERE response_id IN (SELECT id FROM responses WHERE task_id=?)",(row["task_id"],))
        else:
            task_status="open"
            conn.execute("UPDATE tasks SET status='open',assignee_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(row["task_id"],))
            conn.execute("UPDATE responses SET status='withdrawn',updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND status='accepted'",(row["task_id"],))
            conn.execute("UPDATE task_cancel_requests SET status='rejected',resolved_at=CURRENT_TIMESTAMP WHERE task_id=? AND status='pending'",(row["task_id"],))
            conn.execute("UPDATE chats SET closed_at=CURRENT_TIMESTAMP WHERE response_id IN (SELECT id FROM responses WHERE task_id=?)",(row["task_id"],))
        conn.execute("UPDATE disputes SET status='resolved',admin_decision=?,admin_comment=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                     (data.decision,data.comment.strip(),dispute_id))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"resolve_dispute","dispute",dispute_id,f"{data.decision};{data.comment.strip()}"))
    notice=f"Модератор рассмотрел жалобу по сделке «{row['title']}». Решение: {data.decision}. {data.comment}".strip()
    await notify_telegram(row["owner_telegram_id"],notice)
    if row["executor_telegram_id"]: await notify_telegram(row["executor_telegram_id"],notice)
    return {"ok":True,"status":task_status}


@app.get("/api/moderation/disputes/{dispute_id}/chat")
def moderator_dispute_chat(dispute_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data);moderator(account)
    with connect() as conn:
        dispute=conn.execute("SELECT task_id FROM disputes WHERE id=?",(dispute_id,)).fetchone()
        if not dispute: raise HTTPException(404,"Жалоба не найдена")
        rows=conn.execute("""SELECT m.*,u.first_name sender_name FROM messages m
            JOIN chats c ON c.id=m.chat_id JOIN responses r ON r.id=c.response_id
            JOIN users u ON u.id=m.sender_id WHERE r.task_id=? ORDER BY m.id""",(dispute["task_id"],)).fetchall()
    return [dict(x) for x in rows]


@app.get("/api/market/chats")
def list_market_chats(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        rows=conn.execute("""SELECT c.id chat_id,c.created_at,c.closed_at,r.id response_id,t.id task_id,t.title,
            t.created_by,r.executor_id,owner.first_name customer_name,executor.first_name executor_name
            FROM chats c JOIN responses r ON r.id=c.response_id JOIN tasks t ON t.id=r.task_id
            JOIN users owner ON owner.id=t.created_by JOIN users executor ON executor.id=r.executor_id
            WHERE t.created_by=? OR r.executor_id=? ORDER BY c.id DESC""",(account["id"],account["id"])).fetchall()
    return [dict(x) for x in rows]


@app.get("/api/market/chats/{chat_id}/messages")
def get_market_messages(chat_id: int, after_id: int = 0, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        chat=conn.execute("""SELECT c.id,t.created_by,r.executor_id FROM chats c
            JOIN responses r ON r.id=c.response_id JOIN tasks t ON t.id=r.task_id WHERE c.id=?""",(chat_id,)).fetchone()
        if not chat or int(account["id"]) not in (int(chat["created_by"]),int(chat["executor_id"])):
            raise HTTPException(404,"Чат не найден")
        rows=conn.execute("""SELECT m.*,u.first_name sender_name FROM messages m JOIN users u ON u.id=m.sender_id
            WHERE m.chat_id=? AND m.id>? AND (m.hidden_for_user_id IS NULL OR m.hidden_for_user_id<>?) AND NOT EXISTS (SELECT 1 FROM message_hides h WHERE h.message_id=m.id AND h.user_id=?)
            ORDER BY m.id LIMIT 100""",(chat_id,after_id,account["id"],account["id"])).fetchall()
        conn.execute("""UPDATE messages SET read_at=CURRENT_TIMESTAMP WHERE chat_id=? AND sender_id<>? AND read_at IS NULL""",(chat_id,account["id"]))
    result=[]
    for row in rows:
        item=dict(row)
        if int(item["sender_id"])!=int(account["id"]):
            item["text"]=redact_chat_contacts(item["text"])
        result.append(item)
    return result


@app.post("/api/market/chats/{chat_id}/messages")
async def send_market_message(chat_id: int, data: SubmitIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    message=data.body.strip()
    if not message or len(message)>2000: raise HTTPException(422,"Сообщение должно быть от 1 до 2000 символов")
    import re
    warning=bool(re.search(r"(?:https?://|t\.me/|@[A-Za-z0-9_]{5,}|\+?\d[\d\s()\-]{8,})",message,re.I))
    with connect() as conn:
        chat=conn.execute("""SELECT c.id,c.closed_at,t.created_by,r.executor_id,t.title,
            owner.telegram_id owner_telegram_id,executor.telegram_id executor_telegram_id,
            (SELECT last_seen FROM user_presence WHERE user_id=CASE WHEN ?=t.created_by THEN r.executor_id ELSE t.created_by END) recipient_last_seen
            FROM chats c JOIN responses r ON r.id=c.response_id JOIN tasks t ON t.id=r.task_id
            JOIN users owner ON owner.id=t.created_by JOIN users executor ON executor.id=r.executor_id
            WHERE c.id=?""",(account["id"],chat_id)).fetchone()
        if not chat or int(account["id"]) not in (int(chat["created_by"]),int(chat["executor_id"])):
            raise HTTPException(404,"Чат не найден")
        if chat["closed_at"]:
            raise HTTPException(409,"Сделка завершена. Чат доступен только для чтения.")
        cur=conn.execute("INSERT INTO messages(chat_id,sender_id,text) VALUES(?,?,?)",(chat_id,account["id"],message))
        recipient_id=chat["executor_id"] if int(account["id"])==int(chat["created_by"]) else chat["created_by"]
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"send_chat_message","chat",chat_id,f"message_id={cur.lastrowid}"))
    recipient_telegram=chat["executor_telegram_id"] if int(account["id"])==int(chat["created_by"]) else chat["owner_telegram_id"]
    last_seen=chat["recipient_last_seen"]
    is_online=bool(last_seen and (datetime.utcnow()-datetime.strptime(last_seen,"%Y-%m-%d %H:%M:%S")).total_seconds()<600)
    if not is_online:
        await notify_telegram(recipient_telegram,f"💬 Новое сообщение по заданию «{chat['title']}»")
    return {"id":cur.lastrowid,"warning_external_contact":warning}


@app.post("/api/market/tasks/{task_id}/cancel-request/reject")
async def reject_market_cancellation(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("""SELECT t.id,t.title,t.created_by,r.executor_id,
            cr.requested_by,owner.telegram_id owner_telegram_id,executor.telegram_id executor_telegram_id
            FROM tasks t JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            JOIN users owner ON owner.id=t.created_by JOIN users executor ON executor.id=r.executor_id
            JOIN task_cancel_requests cr ON cr.task_id=t.id AND cr.status='pending'
            WHERE t.id=? AND t.status='in_progress'""",(task_id,)).fetchone()
        if not task or int(account["id"]) not in (int(task["created_by"]),int(task["executor_id"])):
            raise HTTPException(404,"Запрос на отмену не найден")
        if int(task["requested_by"])==int(account["id"]):
            raise HTTPException(409,"Нельзя отклонить собственный запрос")
        conn.execute("UPDATE task_cancel_requests SET status='rejected',accepted_by=?,resolved_at=CURRENT_TIMESTAMP WHERE task_id=? AND status='pending'",(account["id"],task_id))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(account["id"],"reject_mutual_cancellation","task",task_id))
    requester=task["executor_telegram_id"] if int(task["requested_by"])==int(task["executor_id"]) else task["owner_telegram_id"]
    await notify_telegram(requester,f"Вторая сторона отклонила запрос на отмену сделки «{task['title']}».")
    return {"ok":True,"status":"rejected"}


@app.post("/api/market/tasks/{task_id}/cancel-request")
async def request_market_cancellation(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("""SELECT t.id,t.title,t.created_by,t.status,r.executor_id,
            owner.telegram_id owner_telegram_id,executor.telegram_id executor_telegram_id
            FROM tasks t JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            JOIN users owner ON owner.id=t.created_by JOIN users executor ON executor.id=r.executor_id
            WHERE t.id=? AND t.room_id IS NULL""",(task_id,)).fetchone()
        if not task or task["status"]!="in_progress" or int(account["id"]) not in (int(task["created_by"]),int(task["executor_id"])):
            raise HTTPException(409,"Взаимная отмена доступна только участникам активной сделки")
        existing=conn.execute("SELECT * FROM task_cancel_requests WHERE task_id=?",(task_id,)).fetchone()
        if existing and existing["status"]=="pending":
            if int(existing["requested_by"])==int(account["id"]):
                raise HTTPException(409,"Ты уже отправил запрос на отмену. Ждём второго участника.")
            conn.execute("UPDATE task_cancel_requests SET accepted_by=?,status='accepted',resolved_at=CURRENT_TIMESTAMP WHERE task_id=?",
                         (account["id"],task_id))
            conn.execute("UPDATE tasks SET status='cancelled',assignee_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='in_progress'",(task_id,))
            conn.execute("UPDATE chats SET closed_at=CURRENT_TIMESTAMP WHERE response_id=(SELECT id FROM responses WHERE task_id=? AND status='accepted')",(task_id,))
            result="cancelled"
        else:
            conn.execute("""INSERT INTO task_cancel_requests(task_id,requested_by,accepted_by,status)
                VALUES(?,?,NULL,'pending') ON CONFLICT(task_id) DO UPDATE SET
                requested_by=excluded.requested_by,accepted_by=NULL,status='pending',
                created_at=CURRENT_TIMESTAMP,resolved_at=NULL""",(task_id,account["id"]))
            result="pending"
        recipient=task["executor_telegram_id"] if int(account["id"])==int(task["created_by"]) else task["owner_telegram_id"]
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"request_mutual_cancellation","task",task_id,result))
    if result=="pending":
        await notify_telegram(recipient,f"Запрошена взаимная отмена сделки «{task['title']}». Открой TaskHub и подтверди или отклони запрос.")
    else:
        await notify_telegram(task["owner_telegram_id"],f"Сделка «{task['title']}» отменена по взаимному согласию.")
        await notify_telegram(task["executor_telegram_id"],f"Сделка «{task['title']}» отменена по взаимному согласию.")
    return {"ok":True,"status":result}


@app.post("/api/market/tasks/{task_id}/complete")
async def complete_market_task(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("""SELECT t.*,r.id response_id,executor.telegram_id executor_telegram_id,
            owner.telegram_id owner_telegram_id FROM tasks t
            JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            JOIN users executor ON executor.id=r.executor_id
            JOIN users owner ON owner.id=t.created_by
            WHERE t.id=? AND t.created_by=? AND t.room_id IS NULL AND t.status='in_progress'""",(task_id,account["id"])).fetchone()
        if not task: raise HTTPException(404,"Активная сделка не найдена")
        conn.execute("UPDATE tasks SET status='done',completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,))
        conn.execute("UPDATE chats SET closed_at=CURRENT_TIMESTAMP WHERE response_id=?",(task["response_id"],))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(account["id"],"complete_market_task","task",task_id))
    reminder=f"🎉 Сделка «{task['title']}» завершена. Не забудьте оставить друг другу отзыв в TaskHub."
    await notify_telegram(task["executor_telegram_id"],reminder)
    await notify_telegram(task["owner_telegram_id"],reminder)
    return {"ok":True,"status":"done"}


@app.post("/api/market/tasks/{task_id}/reviews")
def create_market_review(task_id: int, data: MarketReviewIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("""SELECT t.created_by,r.executor_id,t.status FROM tasks t
            JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            WHERE t.id=? AND t.room_id IS NULL""",(task_id,)).fetchone()
        if not task or task["status"]!="done": raise HTTPException(409,"Отзыв доступен после завершения сделки")
        if int(account["id"])==int(task["created_by"]): target=task["executor_id"]
        elif int(account["id"])==int(task["executor_id"]): target=task["created_by"]
        else: raise HTTPException(403,"Оставить отзыв могут только участники сделки")
        try:
            cur=conn.execute("INSERT INTO reviews(task_id,author_id,target_id,rating,text) VALUES(?,?,?,?,?)",(task_id,account["id"],target,data.rating,data.text.strip()))
        except Exception as exc:
            if "UNIQUE" in str(exc): raise HTTPException(409,"Ты уже оставил отзыв по этой сделке")
            raise
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(account["id"],"create_review","review",cur.lastrowid))
    return {"ok":True,"id":cur.lastrowid}


@app.post("/api/market/tasks/{task_id}/disputes")
def create_market_dispute(task_id: int, data: MarketDisputeIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        task=conn.execute("""SELECT t.*,r.executor_id FROM tasks t JOIN responses r ON r.task_id=t.id AND r.status='accepted'
            WHERE t.id=? AND t.room_id IS NULL""",(task_id,)).fetchone()
        if not task or int(account["id"]) not in (int(task["created_by"]),int(task["executor_id"])):
            raise HTTPException(404,"Сделка не найдена")
        if task["status"] not in ("in_progress","done"): raise HTTPException(409,"Жалоба недоступна для этого статуса")
        if task["status"]=="done" and task["completed_at"]:
            expired=conn.execute("SELECT datetime(?) < datetime('now','-7 days') expired",(task["completed_at"],)).fetchone()["expired"]
            if expired: raise HTTPException(409,"Срок подачи жалобы после завершения сделки — 7 дней")
        conn.execute("INSERT INTO disputes(task_id,user_id,reason,status) VALUES(?,?,?,'open')",(task_id,account["id"],data.reason.strip()))
        conn.execute("UPDATE tasks SET status='disputed',dispute_status='open',updated_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"open_dispute","task",task_id,data.reason.strip()))
    return {"ok":True,"status":"disputed"}


@app.post("/api/market/tasks/{task_id}/claim")
def claim_market_task(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410, "На бирже нельзя сразу забрать задание. Сначала отправь отклик, заказчик выберет исполнителя.")


@app.post("/api/market/tasks/{task_id}/submit")
def submit_market_task(task_id: int, data: SubmitIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410, "Сдача работы через старую форму отключена. Используй чат сделки; завершение подтверждает заказчик.")


@app.post("/api/market/tasks/{task_id}/review")
def review_market_task(task_id: int, data: ReviewIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    raise HTTPException(410, "Старая проверка качества не используется в MVP. Завершение сделки подтверждается заказчиком.")


@app.get("/api/tasks")
def tasks(status: str = "available", x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        if status=="mine": rows=conn.execute("SELECT * FROM tasks WHERE assignee_id=? ORDER BY id DESC",(account["id"],)).fetchall()
        elif status=="all":
            admin(account)
            rows=conn.execute("SELECT t.*,u.first_name assignee_name,cu.first_name creator_name FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id LEFT JOIN users cu ON cu.id=t.created_by ORDER BY t.id DESC").fetchall()
        elif status=="available": rows=conn.execute("SELECT * FROM tasks WHERE status='available' ORDER BY id DESC").fetchall()
        elif status in {"in_progress","submitted","revision","accepted","rejected","cancelled","not_completed"}:
            admin(account); rows=conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC",(status,)).fetchall()
        else: raise HTTPException(400,"Неизвестный фильтр задач")
    return [dict(r) for r in rows]


@app.post("/api/tasks")
def create_task(data:TaskIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    if not data.title.strip() or not data.category.strip(): raise HTTPException(422,"Название и категория обязательны")
    reward=task_base_reward(data)
    with connect() as conn:
        project=None; stage=None
        if data.stage_id:
            stage=conn.execute("SELECT * FROM stages WHERE id=?",(data.stage_id,)).fetchone()
            if not stage: raise HTTPException(404,"Этап не найден")
            if data.project_id and data.project_id!=stage["project_id"]: raise HTTPException(400,"Этап не принадлежит проекту")
            project=conn.execute("SELECT * FROM projects WHERE id=?",(stage["project_id"],)).fetchone()
        elif data.project_id:
            project=conn.execute("SELECT * FROM projects WHERE id=?",(data.project_id,)).fetchone()
            if not project: raise HTTPException(404,"Проект не найден")
        if data.tour_id:
            raise HTTPException(400,"Туры будут включены следующим этапом миграции")
        if stage:
            reserved=conn.execute("SELECT COALESCE(SUM(budget_reserved_kop),0) n FROM tasks WHERE stage_id=? AND status NOT IN ('cancelled','rejected')",(stage["id"],)).fetchone()["n"]
            if reserved+reward>stage["budget_kop"]: raise HTTPException(409,"Не хватает бюджета этапа для резерва задачи")
        if project and not stage:
            reserved=conn.execute("SELECT COALESCE(SUM(budget_reserved_kop),0) n FROM tasks WHERE project_id=? AND status NOT IN ('cancelled','rejected')",(project["id"],)).fetchone()["n"]
            if reserved+reward>project["budget_kop"]: raise HTTPException(409,"Не хватает бюджета проекта")
        cur=conn.execute(
            """INSERT INTO tasks(title,description,category,reward_kop,created_by,deadline,project_id,stage_id,tour_id,
            pricing_model,norm_minutes,difficulty,urgency,fixed_reward_kop,budget_reserved_kop,status,moderation_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.title.strip(),data.description.strip(),data.category.strip(),reward,account["id"],data.deadline,
             project["id"] if project else None,stage["id"] if stage else None,data.tour_id,data.pricing_model,
             data.norm_minutes,data.difficulty,data.urgency,reward,reward,"pending","pending")
        )
        for pos,item in enumerate(data.criteria):
            conn.execute("INSERT INTO task_criteria(task_id,title,weight,critical,position) VALUES(?,?,?,?,?)",
                         (cur.lastrowid,item.title.strip(),item.weight,int(item.critical),pos))
        conn.execute("UPDATE tasks SET criteria_frozen=1 WHERE id=?",(cur.lastrowid,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],"submit_for_moderation","task",cur.lastrowid,
                      f"pricing={data.pricing_model};norm_minutes={data.norm_minutes};difficulty={data.difficulty};urgency={data.urgency};reward_kop={reward}"))
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?",(cur.lastrowid,)).fetchone())


@app.get("/api/tasks/{task_id}/criteria")
def get_criteria(task_id:int,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    user(x_telegram_init_data)
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM task_criteria WHERE task_id=? ORDER BY position,id",(task_id,))]


@app.post("/api/tasks/{task_id}/claim")
async def claim(task_id:int,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    if account["role"]=="admin": raise HTTPException(403,"Администратор не может брать задачи исполнителя")
    with connect() as conn:
        task=conn.execute("SELECT t.title,t.created_by,creator.telegram_id creator_telegram_id FROM tasks t LEFT JOIN users creator ON creator.id=t.created_by WHERE t.id=? AND t.status='available' AND t.moderation_status='approved'",(task_id,)).fetchone()
        if not task: raise HTTPException(409,"Задача ещё не одобрена, уже занята или недоступна")
        if int(task["created_by"]) == int(account["id"]):
            raise HTTPException(403,"Нельзя взять собственную задачу. Её должен выполнить другой участник.")
        cur=conn.execute("UPDATE tasks SET status='in_progress',assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='available'",(account["id"],task_id))
        if cur.rowcount!=1: raise HTTPException(409,"Задача уже занята или недоступна")
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",(account["id"],"claim","task",task_id))
    await notify_telegram(task["creator_telegram_id"],f"👀 Твою задачу взяли в работу!\\n\\n«{task['title']}»\\nИсполнитель: {account.get('first_name') or 'Участник'}")
    return {"ok":True}


@app.post("/api/tasks/{task_id}/submit")
async def submit(task_id:int,data:SubmitIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    if not data.body.strip(): raise HTTPException(422,"Добавь описание результата или ссылку")
    with connect() as conn:
        task=conn.execute("SELECT t.title,creator.telegram_id creator_telegram_id FROM tasks t LEFT JOIN users creator ON creator.id=t.created_by WHERE t.id=? AND t.assignee_id=? AND t.status IN ('in_progress','revision')",(task_id,account["id"])).fetchone()
        if not task: raise HTTPException(409,"Нельзя сдать эту задачу")
        cur=conn.execute("INSERT INTO submissions(task_id,user_id,body) VALUES(?,?,?)",(task_id,account["id"],data.body.strip()))
        conn.execute("UPDATE tasks SET status='submitted',updated_at=CURRENT_TIMESTAMP WHERE id=?",(task_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",(account["id"],"submit","submission",cur.lastrowid,f"task_id={task_id}"))
    await notify_telegram(task["creator_telegram_id"],f"📨 Работа по твоей задаче отправлена на проверку!\\n\\n«{task['title']}»\\nИсполнитель: {account.get('first_name') or 'Участник'}")
    return {"ok":True}


@app.get("/api/moderation")
def moderation_queue(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data); admin(account)
    with connect() as conn:
        rows=conn.execute("""SELECT t.*,u.first_name creator_name,u.username creator_username
            FROM tasks t LEFT JOIN users u ON u.id=t.created_by
            WHERE t.moderation_status='pending' ORDER BY t.created_at""").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/tasks/{task_id}/moderate")
async def moderate_task(task_id:int,data:ModerationIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data); admin(account)
    if data.decision=="reject" and not data.comment.strip():
        raise HTTPException(400,"Укажи причину отклонения")
    with connect() as conn:
        task=conn.execute("SELECT t.title,u.telegram_id creator_chat FROM tasks t LEFT JOIN users u ON u.id=t.created_by WHERE t.id=? AND t.moderation_status='pending'",(task_id,)).fetchone()
        if not task: raise HTTPException(404,"Задача не найдена или уже рассмотрена")
        approved=data.decision=="approve"
        conn.execute("""UPDATE tasks SET status=?,moderation_status=?,moderation_comment=?,moderated_by=?,moderated_at=CURRENT_TIMESTAMP
            WHERE id=? AND moderation_status='pending'""",
            ("available" if approved else "rejected","approved" if approved else "rejected",data.comment.strip(),account["id"],task_id))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"],"moderation_"+data.decision,"task",task_id,data.comment.strip()))
    await notify_telegram(task["creator_chat"],("✅ Задача одобрена и опубликована" if approved else "❌ Задача отклонена")+f"\n\n«{task['title']}»"+(f"\nПричина: {data.comment.strip()}" if data.comment.strip() else ""))
    return {"ok":True,"status":"available" if approved else "rejected"}


@app.get("/api/review")
def review(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data); admin(account)
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT t.*,u.first_name assignee_name,s.id submission_id,s.body submission,s.created_at submitted_at
            FROM tasks t JOIN users u ON u.id=t.assignee_id
            LEFT JOIN submissions s ON s.id=(SELECT MAX(id) FROM submissions WHERE task_id=t.id)
            WHERE t.status='submitted' AND t.assignee_id<>? ORDER BY t.id""",(account["id"],))]


@app.post("/api/tasks/{task_id}/review")
def decide(task_id:int,data:ReviewIn,x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data); admin(account)
    if data.decision not in ("accept","revision","reject"): raise HTTPException(400,"Неизвестное решение")
    if data.decision in ("revision","reject") and not data.comment.strip(): raise HTTPException(400,"Укажи причину")
    if data.quality not in QUALITY_FACTORS: raise HTTPException(400,"Неизвестная оценка качества")
    status={"accept":"accepted","revision":"revision","reject":"rejected"}[data.decision]
    with connect() as conn:
        task=conn.execute("SELECT * FROM tasks WHERE id=? AND status='submitted'",(task_id,)).fetchone()
        if not task: raise HTTPException(409,"Работа уже рассмотрена или не найдена")
        if int(task["assignee_id"]) == int(account["id"]):
            raise HTTPException(403,"Нельзя проверять и принимать собственную работу.")
        cur=conn.execute("UPDATE tasks SET status=?,quality_factor=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='submitted'",(status,QUALITY_FACTORS[data.quality],task_id))
        if cur.rowcount!=1: raise HTTPException(409,"Работа уже рассмотрена")
        if data.decision=="accept":
            amount=int(round(task["reward_kop"]*QUALITY_FACTORS[data.quality]))
            conn.execute("INSERT INTO ledger(user_id,task_id,amount_kop,kind,note) VALUES(?,?,?,?,?)",
                         (task["assignee_id"],task_id,amount,"task_earning",f"Принято · качество: {data.quality} · {data.comment.strip()}"))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
                     (account["id"],data.decision,"task",task_id,f"quality={data.quality};{data.comment.strip()}"))
    return {"ok":True,"status":status}


@app.get("/api/ledger")
def ledger(x_telegram_init_data:str=Header(default="",alias="X-Telegram-Init-Data")):
    account=user(x_telegram_init_data)
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT l.*,t.title FROM ledger l LEFT JOIN tasks t ON t.id=l.task_id WHERE l.user_id=? ORDER BY l.id DESC",(account["id"],))]
