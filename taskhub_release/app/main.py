from __future__ import annotations

import asyncio
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

from .auth import verify_telegram_init_data
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
    log.info("TASKHUB_API_STARTED bot_status=%s", _bot_status)
    try:
        yield
    finally:
        expiry_task.cancel()
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
    path = request.url.path
    allowed = {
        "/api/me", "/api/health", "/api/market/profile",
        "/api/market/tasks", "/api/market/ledger",
    }
    if path.startswith("/api/market/tasks/"):
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
            return JSONResponse(status_code=410, content={"detail": "Этот адрес относится к старому API. Используйте API комнаты или биржи."})
    return await call_next(request)







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
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=10000)
    category: str = Field(default="Другое", min_length=1, max_length=60)
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


def current_user(init_data: str):
    token = getenv("BOT_TOKEN", "").strip()
    telegram_user = verify_telegram_init_data(init_data, token)
    dev_mode = getenv("DEV_MODE", "false").lower() == "true"
    if not telegram_user and dev_mode:
        telegram_user = {"id": 999000, "first_name": "Разработчик", "username": "dev"}
    if not telegram_user:
        raise HTTPException(401, "Откройте TaskHub через Telegram")
    telegram_id = int(telegram_user["id"])
    admin_ids = {int(value.strip()) for value in getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip().isdigit()}
    role = "admin" if telegram_id in admin_ids or (dev_mode and telegram_id == 999000) else "helper"
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


def task_base_reward(data: TaskIn) -> int:
    if data.pricing_model == "fixed":
        return int(data.reward_rub or 0) * 100
    amount = BASE_RATE_KOP_PER_HOUR * data.norm_minutes / 60 * data.difficulty * data.urgency
    return int(round(amount))


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
        migrated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rooms'"
        ).fetchone() is not None
        if migrated:
            # Financial totals and task counts must always be requested with a room context.
            return {"user": account, "balance_kop": 0, "counts": {}}
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM tasks WHERE assignee_id=? GROUP BY status", (account["id"],))}
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


@app.get("/api/market/profile")
def market_profile(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        balance = conn.execute("SELECT COALESCE(SUM(amount_kop),0) n FROM ledger WHERE user_id=?", (account["id"],)).fetchone()["n"]
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,COUNT(*) n FROM tasks WHERE assignee_id=? GROUP BY status", (account["id"],)
        )}
        created = conn.execute("SELECT COUNT(*) n FROM tasks WHERE created_by=?", (account["id"],)).fetchone()["n"]
        mine = conn.execute(
            "SELECT * FROM tasks WHERE created_by=? OR assignee_id=? ORDER BY id DESC LIMIT 100",
            (account["id"], account["id"]),
        ).fetchall()
    return {"user": account, "balance_kop": balance, "counts": counts, "created_count": created, "tasks": [dict(r) for r in mine]}


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
def market_tasks(status: str = "available", x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        if status == "mine":
            rows = conn.execute(
                "SELECT * FROM tasks WHERE room_id IS NULL AND (created_by=? OR assignee_id=?) ORDER BY id DESC",
                (account["id"], account["id"]),
            ).fetchall()
        elif status == "available":
            rows = conn.execute(
                "SELECT t.*,u.first_name creator_name FROM tasks t LEFT JOIN users u ON u.id=t.created_by WHERE t.room_id IS NULL AND t.status='available' AND t.moderation_status='approved' ORDER BY t.id DESC"
            ).fetchall()
        else:
            raise HTTPException(400, "Неизвестный фильтр биржи")
    return [dict(row) for row in rows]


@app.post("/api/market/tasks")
async async def create_market_task(data: TaskIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if not data.title.strip() or not data.category.strip():
        raise HTTPException(422, "Название и категория обязательны")
    if data.project_id or data.stage_id or data.tour_id:
        raise HTTPException(400, "Публичные задания биржи не привязываются к проектам комнаты")
    reward = task_base_reward(data)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO tasks(title,description,category,reward_kop,created_by,deadline,
               pricing_model,norm_minutes,difficulty,urgency,fixed_reward_kop,budget_reserved_kop,
               status,moderation_status,room_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (data.title.strip(), data.description.strip(), data.category.strip(), reward,
             account["id"], data.deadline, data.pricing_model, data.norm_minutes,
             data.difficulty, data.urgency, reward, 0, "available", "approved"),
        )
        task_id = cur.lastrowid
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
        import json
        subscribers = conn.execute("SELECT u.telegram_id,p.categories FROM market_notification_preferences p JOIN users u ON u.id=p.user_id WHERE p.enabled=1 AND p.user_id<>?", (account["id"],)).fetchall()
    for subscriber in subscribers:
        try:
            subscribed = json.loads(subscriber["categories"] or "[]")
        except Exception:
            subscribed = []
        if any(task_row["category"] == c or task_row["category"].startswith(c + " / ") for c in subscribed):
            await notify_telegram(subscriber["telegram_id"], f"🆕 Новое задание в категории «{task_row['category']}»: {task_row['title']} · {task_row['reward_kop']/100:.0f} ₽")
    return task_row


@app.put("/api/market/tasks/{task_id}")
def update_market_task(task_id: int, data: MarketTaskUpdate, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.project_id or data.stage_id or data.tour_id:
        raise HTTPException(400, "Публичные задания не привязываются к проектам комнаты")
    reward = task_base_reward(data)
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND room_id IS NULL AND created_by=?", (task_id, account["id"])).fetchone()
        if not task:
            raise HTTPException(404, "Твоё задание не найдено")
        if task["status"] != "available":
            raise HTTPException(409, "Редактировать можно только открытое задание. Сначала заверши или отмени текущую работу.")
        conn.execute("""UPDATE tasks SET title=?,description=?,category=?,reward_kop=?,deadline=?,pricing_model=?,norm_minutes=?,difficulty=?,urgency=?,fixed_reward_kop=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND created_by=? AND room_id IS NULL AND status='available'""",
            (data.title.strip(), data.description.strip(), data.category.strip(), reward, data.deadline, data.pricing_model, data.norm_minutes, data.difficulty, data.urgency, reward, task_id, account["id"]))
        conn.execute("DELETE FROM task_criteria WHERE task_id=?", (task_id,))
        for pos, item in enumerate(data.criteria):
            conn.execute("INSERT INTO task_criteria(task_id,title,weight,critical,position) VALUES(?,?,?,?,?)", (task_id, item.title.strip(), item.weight, int(item.critical), pos))
        conn.execute("UPDATE tasks SET criteria_frozen=1 WHERE id=?", (task_id,))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "edit_market_task", "task", task_id, "public_marketplace"))
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


@app.delete("/api/market/tasks/{task_id}")
def delete_market_task(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND room_id IS NULL AND created_by=?", (task_id, account["id"])).fetchone()
        if not task:
            raise HTTPException(404, "Твоё задание не найдено")
        if task["status"] != "available" or task["assignee_id"] is not None:
            raise HTTPException(409, "Нельзя удалить задание, которое уже взяли в работу или начали выполнять")
        conn.execute("UPDATE tasks SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND created_by=? AND status='available'", (task_id, account["id"]))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "delete_market_task", "task", task_id, "soft_delete"))
    return {"ok": True}


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
    with connect() as conn:
        rows = conn.execute("""SELECT s.*,u.first_name seller_name,u.username seller_username,
            (SELECT COUNT(*) FROM market_service_orders o WHERE o.service_id=s.id AND o.status NOT IN ('cancelled','completed')) active_orders
            FROM market_services s JOIN users u ON u.id=s.seller_id WHERE s.active=1 ORDER BY s.id DESC""").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/market/services")
def create_market_service(data: MarketServiceIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        cur = conn.execute("INSERT INTO market_services(seller_id,title,description,category,price_kop,delivery_minutes) VALUES(?,?,?,?,?,?)",
            (account["id"], data.title.strip(), data.description.strip(), data.category.strip(), data.price_rub*100, data.delivery_minutes))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "create_market_service", "market_service", cur.lastrowid, "public_marketplace"))
        return dict(conn.execute("SELECT * FROM market_services WHERE id=?", (cur.lastrowid,)).fetchone())


@app.post("/api/market/services/{service_id}/orders")
async def create_market_service_order(service_id: int, data: MarketServiceOrderIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        service = conn.execute("SELECT s.*,u.telegram_id seller_telegram_id FROM market_services s JOIN users u ON u.id=s.seller_id WHERE s.id=? AND s.active=1", (service_id,)).fetchone()
        if not service:
            raise HTTPException(404, "Услуга больше недоступна")
        if int(service["seller_id"]) == int(account["id"]):
            raise HTTPException(403, "Нельзя заказать собственную услугу")
        cur = conn.execute("INSERT INTO market_service_orders(service_id,buyer_id,seller_id,details,price_kop) VALUES(?,?,?,?,?)",
            (service_id, account["id"], service["seller_id"], data.details.strip(), service["price_kop"]))
        order_id = cur.lastrowid
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "create_market_service_order", "market_service_order", order_id, f"service_id={service_id}"))
    await notify_telegram(service["seller_telegram_id"], f"🛒 Новый заказ услуги «{service['title']}» #{order_id}. Открой TaskHub, чтобы ответить.")
    return {"ok": True, "order_id": order_id, "status": "created"}


@app.get("/api/market/service-orders")
def market_service_orders(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        rows = conn.execute("""SELECT o.*,s.title service_title,s.category,b.first_name buyer_name,sel.first_name seller_name
            FROM market_service_orders o JOIN market_services s ON s.id=o.service_id
            JOIN users b ON b.id=o.buyer_id JOIN users sel ON sel.id=o.seller_id
            WHERE o.buyer_id=? OR o.seller_id=? ORDER BY o.id DESC LIMIT 100""", (account["id"], account["id"])).fetchall()
    return [dict(r) for r in rows]


@app.patch("/api/market/service-orders/{order_id}")
async def update_market_service_order(order_id: int, data: MarketServiceOrderStatusIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        order = conn.execute("SELECT o.*,b.telegram_id buyer_telegram_id,seller.telegram_id seller_telegram_id,s.title service_title FROM market_service_orders o JOIN users b ON b.id=o.buyer_id JOIN users seller ON seller.id=o.seller_id JOIN market_services s ON s.id=o.service_id WHERE o.id=?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(404, "Заказ услуги не найден")
        old = order["status"]
        if data.status == "accepted" and int(order["seller_id"]) == int(account["id"]) and old == "created":
            pass
        elif data.status == "delivered" and int(order["seller_id"]) == int(account["id"]) and old == "accepted":
            pass
        elif data.status == "completed" and int(order["buyer_id"]) == int(account["id"]) and old == "delivered":
            pass
        elif data.status == "cancelled" and int(order["buyer_id"]) == int(account["id"]) and old == "created":
            pass
        else:
            raise HTTPException(403, "Это действие недоступно для твоей роли или текущего статуса заказа")
        conn.execute("UPDATE market_service_orders SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status=?", (data.status, order_id, old))
        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)", (account["id"], "update_market_service_order", "market_service_order", order_id, f"{old}->{data.status}"))
    recipient = order["buyer_telegram_id"] if int(order["seller_id"]) == int(account["id"]) else order["seller_telegram_id"]
    await notify_telegram(recipient, f"📦 Заказ услуги «{order['service_title']}» #{order_id}: статус «{data.status}».")
    return {"ok": True, "status": data.status}


@app.post("/api/market/tasks/{task_id}/claim")
async def claim_market_task(task_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute(
            """SELECT t.*,u.telegram_id creator_telegram_id FROM tasks t
               LEFT JOIN users u ON u.id=t.created_by
               WHERE t.id=? AND t.room_id IS NULL AND t.status='available'
               AND t.moderation_status='approved'""", (task_id,)
        ).fetchone()
        if not task:
            raise HTTPException(409, "Задача уже занята или недоступна")
        if int(task["created_by"]) == int(account["id"]):
            raise HTTPException(403, "Нельзя взять собственную задачу")
        cur = conn.execute(
            "UPDATE tasks SET status='in_progress',assignee_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND room_id IS NULL AND status='available'",
            (account["id"], task_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Задача уже занята")
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id) VALUES(?,?,?,?)",
            (account["id"], "claim_market_task", "task", task_id),
        )
    await notify_telegram(task["creator_telegram_id"], f"👀 Твою задачу на бирже взяли в работу: «{task['title']}»")
    return {"ok": True, "status": "in_progress"}


@app.post("/api/market/tasks/{task_id}/submit")
async def submit_market_task(task_id: int, data: SubmitIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        task = conn.execute(
            "SELECT t.*,u.telegram_id creator_telegram_id FROM tasks t LEFT JOIN users u ON u.id=t.created_by WHERE t.id=? AND t.room_id IS NULL AND t.assignee_id=? AND t.status IN ('in_progress','revision')",
            (task_id, account["id"]),
        ).fetchone()
        if not task:
            raise HTTPException(409, "Нельзя сдать эту задачу")
        cur = conn.execute("INSERT INTO submissions(task_id,user_id,body) VALUES(?,?,?)", (task_id, account["id"], data.body.strip()))
        conn.execute("UPDATE tasks SET status='submitted',updated_at=CURRENT_TIMESTAMP WHERE id=? AND room_id IS NULL", (task_id,))
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "submit_market_task", "submission", cur.lastrowid, f"task_id={task_id}"),
        )
    await notify_telegram(task["creator_telegram_id"], f"📨 Работа по задаче «{task['title']}» отправлена на проверку.")
    return {"ok": True, "status": "submitted"}


@app.post("/api/market/tasks/{task_id}/review")
def review_market_task(task_id: int, data: ReviewIn, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.decision not in ("accept", "revision", "reject"):
        raise HTTPException(400, "Неизвестное решение")
    if data.decision in ("revision", "reject") and not data.comment.strip():
        raise HTTPException(400, "Укажи причину")
    if data.quality not in QUALITY_FACTORS:
        raise HTTPException(400, "Неизвестная оценка качества")
    status = {"accept": "accepted", "revision": "revision", "reject": "rejected"}[data.decision]
    with connect() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND room_id IS NULL AND created_by=? AND status='submitted'",
            (task_id, account["id"]),
        ).fetchone()
        if not task:
            raise HTTPException(404, "Сдача не найдена или принадлежит не тебе")
        cur = conn.execute(
            "UPDATE tasks SET status=?,quality_factor=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND room_id IS NULL AND status='submitted'",
            (status, QUALITY_FACTORS[data.quality], task_id),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "Работа уже рассмотрена")
        if data.decision == "accept":
            amount = int(round(task["reward_kop"] * QUALITY_FACTORS[data.quality]))
            conn.execute(
                "INSERT INTO ledger(user_id,task_id,room_id,amount_kop,kind,note) VALUES(?,?,NULL,?,?,?)",
                (task["assignee_id"], task_id, amount, "task_earning", f"Принято · качество: {data.quality} · {data.comment.strip()}"),
            )
        conn.execute(
            "INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(?,?,?,?,?)",
            (account["id"], "review_market_task", "task", task_id, f"quality={data.quality};{data.comment.strip()}"),
        )
    return {"ok": True, "status": status}


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
