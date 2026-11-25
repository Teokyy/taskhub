"""Transactional room-budget operations. All amounts are integer kopecks."""
from __future__ import annotations

import sqlite3
from .rooms import RoomError


def set_budget_limit(conn: sqlite3.Connection, room_id: int, actor_id: int,
                     limit_kop: int | None) -> dict:
    if limit_kop is not None and limit_kop < 0:
        raise RoomError("Лимит бюджета не может быть отрицательным", 422)
    room = conn.execute("SELECT budget_reserved_kop FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        raise RoomError("Комната не найдена", 404)
    reserved = int(room["budget_reserved_kop"] or 0)
    if limit_kop is not None and limit_kop < reserved:
        raise RoomError(f"Лимит ниже уже зарезервированной суммы ({reserved} коп.)", 409)
    conn.execute("UPDATE rooms SET budget_limit_kop=? WHERE id=?", (limit_kop, room_id))
    conn.execute("""INSERT INTO budget_events(room_id,actor_id,event_type,amount_kop,details)
                    VALUES(?,?,'limit_changed',?,?)""",
                 (room_id, actor_id, limit_kop or 0, "unlimited" if limit_kop is None else ""))
    return get_budget(conn, room_id)


def get_budget(conn: sqlite3.Connection, room_id: int) -> dict:
    row = conn.execute("SELECT budget_limit_kop,budget_reserved_kop FROM rooms WHERE id=?",
                       (room_id,)).fetchone()
    if not row:
        raise RoomError("Комната не найдена", 404)
    limit = row["budget_limit_kop"]
    reserved = int(row["budget_reserved_kop"] or 0)
    return {"limit_kop": limit, "reserved_kop": reserved,
            "available_kop": None if limit is None else int(limit) - reserved}


def reserve_task(conn: sqlite3.Connection, room_id: int, task_id: int,
                 amount_kop: int, actor_id: int | None) -> None:
    if amount_kop < 0:
        raise RoomError("Некорректная сумма резерва", 422)
    task = conn.execute("SELECT budget_reserved_kop FROM tasks WHERE id=? AND room_id=?",
                        (task_id, room_id)).fetchone()
    if not task:
        raise RoomError("Задача не найдена в этой комнате", 404)
    if int(task["budget_reserved_kop"] or 0) > 0:
        return
    cur = conn.execute("""UPDATE rooms SET budget_reserved_kop=budget_reserved_kop+?
        WHERE id=? AND (budget_limit_kop IS NULL OR budget_reserved_kop+?<=budget_limit_kop)""",
        (amount_kop, room_id, amount_kop))
    if cur.rowcount != 1:
        raise RoomError("Недостаточно свободного бюджета комнаты для публикации задачи", 409)
    conn.execute("UPDATE tasks SET budget_reserved_kop=? WHERE id=? AND room_id=?",
                 (amount_kop, task_id, room_id))
    conn.execute("""INSERT INTO budget_events(room_id,task_id,actor_id,event_type,amount_kop)
                    VALUES(?,?,?,'reserved',?)""", (room_id, task_id, actor_id, amount_kop))


def release_task(conn: sqlite3.Connection, room_id: int, task_id: int,
                 actor_id: int | None, event_type: str = "released") -> int:
    if event_type not in ("released", "spent"):
        raise RoomError("Недопустимое событие бюджета", 422)
    task = conn.execute("SELECT budget_reserved_kop FROM tasks WHERE id=? AND room_id=?",
                        (task_id, room_id)).fetchone()
    if not task:
        raise RoomError("Задача не найдена в этой комнате", 404)
    amount = int(task["budget_reserved_kop"] or 0)
    if amount <= 0:
        return 0
    cur = conn.execute("""UPDATE tasks SET budget_reserved_kop=0
        WHERE id=? AND room_id=? AND budget_reserved_kop=?""", (task_id, room_id, amount))
    if cur.rowcount != 1:
        raise RoomError("Резерв задачи уже изменён, обнови страницу", 409)
    conn.execute("UPDATE rooms SET budget_reserved_kop=MAX(0,budget_reserved_kop-?) WHERE id=?",
                 (amount, room_id))
    conn.execute("""INSERT INTO budget_events(room_id,task_id,actor_id,event_type,amount_kop)
                    VALUES(?,?,?,?,?)""", (room_id, task_id, actor_id, event_type, amount))
    return amount
