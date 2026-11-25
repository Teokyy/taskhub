from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date
from os import getenv
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .auth import verify_telegram_init_data
from .db import connect, init_db
from .rooms import (RoomError, create_room as create_room_record, list_rooms, set_work_mode as set_room_work_mode, require_admin as require_room_admin, require_member as require_room_member, require_mode as require_room_mode)

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
                rows=conn.execute("""SELECT t.id,t.title,u.telegram_id assignee_chat,c.telegram_id creator_chat
                    FROM tasks t LEFT JOIN users u ON u.id=t.assignee_id
                    LEFT JOIN users c ON c.id=t.created_by
                    WHERE t.deadline IS NOT NULL AND date(t.deadline)<date('now')
                    AND t.status IN ('available','in_progress','revision')""").fetchall()
                for row in rows:
                    cur=conn.execute("""UPDATE tasks SET status='not_completed',updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status IN ('available','in_progress','revision')""",(row["id"],))
                    if cur.rowcount:
                        conn.execute("INSERT INTO audit(actor_id,action,object_type,object_id,details) VALUES(NULL,?,?,?,?)",
                            ("deadline_expired","task",row["id"],"Срок выполнения истёк"))
                        await notify_telegram(row["creator_chat"],f"⌛ Срок задачи «{row['title']}» истёк. Статус: не выполнена.")
                        if row["assignee_chat"]:
                            await notify_telegram(row["assignee_chat"],f"⌛ Срок задачи «{row['title']}» истёк. Статус: не выполнена.")
        except Exception:
            log.exception("TASK_DEADLINE_SWEEP_FAILED")
        await asyncio.sleep(60)


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
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RoomModeIn(BaseModel):
    mode: str = Field(pattern="^(customer|executor)$")


def ensure_rooms_schema(conn):
    required = {"rooms", "room_members", "room_payout_settings"}
    found = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not required.issubset(found):
        raise HTTPException(503, "Комнаты ещё не включены: сначала выполните проверенную миграцию базы")


def room_error(error: RoomError):
    raise HTTPException(error.status_code, str(error))


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
                      COALESCE((SELECT SUM(t.reward_kop) FROM tasks t
                                WHERE t.project_id=p.id AND t.room_id=p.room_id
                                  AND t.status='accepted'),0) accrued_kop
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
        project = conn.execute(
            "SELECT * FROM projects WHERE id=? AND room_id=?", (project_id, room_id)
        ).fetchone()
        if not project:
            raise HTTPException(404, "Проект не найден в этой комнате")
        allocated = conn.execute(
            "SELECT COALESCE(SUM(budget_kop),0) n FROM stages WHERE project_id=? AND room_id=?",
            (project_id, room_id),
        ).fetchone()["n"]
        if allocated + data.budget_rub * 100 > project["budget_kop"]:
            raise HTTPException(409, "Бюджет этапов превышает бюджет проекта")
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
               WHERE rm.room_id=? AND rm.status='active'
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
def create_room_task(room_id: int, data: TaskIn,
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
        if data.project_id or data.stage_id or data.tour_id:
            raise HTTPException(400, "Привязка к проектам будет подключена следующим этапом")
        status = "pending" if room["moderation_enabled"] else "available"
        moderation = "pending" if room["moderation_enabled"] else "approved"
        reserve = reward if status == "available" else 0
        cur = conn.execute(
            """INSERT INTO tasks(title,description,category,reward_kop,status,created_by,
               deadline,pricing_model,norm_minutes,difficulty,urgency,fixed_reward_kop,
               budget_reserved_kop,moderation_status,criteria_frozen,room_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data.title.strip(), data.description.strip(), data.category.strip(), reward,
             status, account["id"], data.deadline, data.pricing_model, data.norm_minutes,
             data.difficulty, data.urgency, reward, reserve, moderation, 1, room_id),
        )
        for pos, item in enumerate(data.criteria):
            conn.execute(
                "INSERT INTO task_criteria(task_id,title,weight,critical,position) VALUES(?,?,?,?,?)",
                (cur.lastrowid, item.title.strip(), item.weight, int(item.critical), pos),
            )
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?, 'created',NULL,?,?)""",
            (cur.lastrowid, account["id"], status, f"room_id={room_id}"),
        )
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())


@app.post("/api/rooms/{room_id}/tasks/{task_id}/claim")
def claim_room_task(room_id: int, task_id: int,
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
        return {"ok": True, "status": "in_progress"}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/submit")
def submit_room_task(room_id: int, task_id: int, data: SubmitIn,
                     x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    with connect() as conn:
        ensure_rooms_schema(conn)
        try:
            require_room_member(conn, room_id, account["id"])
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
        return {"ok": True, "status": "submitted"}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/moderate")
def moderate_room_task(room_id: int, task_id: int, data: ModerationIn,
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
            # Reserve at publication, never while pending.
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
        return {"ok": True, "status": new_status}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/review")
def review_room_task(room_id: int, task_id: int, data: ReviewIn,
                     x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    account = user(x_telegram_init_data)
    if data.decision not in ("accept", "revision", "reject"):
        raise HTTPException(400, "Неизвестное решение")
    if data.decision in ("revision", "reject") and not data.comment.strip():
        raise HTTPException(400, "Укажи причину")
    if data.quality not in QUALITY_FACTORS:
        raise HTTPException(400, "Неизвестная оценка качества")
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
            conn.execute(
                "UPDATE tasks SET budget_reserved_kop=0 WHERE id=? AND room_id=?",
                (task_id, room_id),
            )
        conn.execute(
            """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
               VALUES(?,?,?,?,?,?)""",
            (task_id, account["id"], data.decision, "submitted", new_status,
             f"quality={data.quality};{data.comment.strip()}"),
        )
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


class SubmitIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class ReviewIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)
    comment: str = Field(default="", max_length=2000)
    quality: str = Field(default="good")


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


class ModerationIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="",max_length=2000)


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
