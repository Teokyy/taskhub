"""Transactional room, project and stage budget operations.

Room budget is the hard global cap. Project stage allocations are ring-fenced
inside the project cap; tasks attached to a stage consume the stage allocation,
while direct project tasks consume the unallocated project balance.
All amounts are integer kopecks.
"""
from __future__ import annotations

import sqlite3
from .rooms import RoomError


def set_budget_limit(conn: sqlite3.Connection, room_id: int, actor_id: int,
                     limit_kop: int | None) -> dict:
    if limit_kop is not None and limit_kop < 0:
        raise RoomError("Лимит бюджета не может быть отрицательным", 422)
    room = conn.execute(
        "SELECT budget_reserved_kop,budget_spent_kop FROM rooms WHERE id=?", (room_id,)
    ).fetchone()
    if not room:
        raise RoomError("Комната не найдена", 404)
    committed = int(room["budget_reserved_kop"] or 0) + int(room["budget_spent_kop"] or 0)
    if limit_kop is not None and limit_kop < committed:
        raise RoomError(f"Лимит ниже уже занятой суммы ({committed} коп.)", 409)
    cur = conn.execute(
        """UPDATE rooms SET budget_limit_kop=?
           WHERE id=? AND (? IS NULL OR budget_reserved_kop+budget_spent_kop<=?)""",
        (limit_kop, room_id, limit_kop, limit_kop),
    )
    if cur.rowcount != 1:
        raise RoomError("Лимит ниже уже занятой суммы", 409)
    conn.execute("""INSERT INTO budget_events(room_id,actor_id,event_type,amount_kop,details)
                    VALUES(?,?,'limit_changed',?,?)""",
                 (room_id, actor_id, limit_kop or 0, "unlimited" if limit_kop is None else ""))
    return get_budget(conn, room_id)


def get_budget(conn: sqlite3.Connection, room_id: int) -> dict:
    row = conn.execute(
        "SELECT budget_limit_kop,budget_reserved_kop,budget_spent_kop FROM rooms WHERE id=?",
        (room_id,),
    ).fetchone()
    if not row:
        raise RoomError("Комната не найдена", 404)
    limit = row["budget_limit_kop"]
    reserved = int(row["budget_reserved_kop"] or 0)
    spent = int(row["budget_spent_kop"] or 0)
    return {"limit_kop": limit, "reserved_kop": reserved, "spent_kop": spent,
            "available_kop": None if limit is None else int(limit) - reserved - spent}


def _task_scope(conn: sqlite3.Connection, room_id: int, task_id: int):
    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND room_id=?", (task_id, room_id)
    ).fetchone()
    if not task:
        raise RoomError("Задача не найдена в этой комнате", 404)
    keys = set(task.keys()) if hasattr(task, "keys") else set()
    project_id = task["project_id"] if "project_id" in keys else None
    stage_id = task["stage_id"] if "stage_id" in keys else None
    stage = None
    if stage_id is not None:
        stage = conn.execute(
            "SELECT * FROM stages WHERE id=? AND room_id=?", (stage_id, room_id)
        ).fetchone()
        if not stage:
            raise RoomError("Этап не найден в этой комнате", 404)
        if project_id is not None and int(stage["project_id"]) != int(project_id):
            raise RoomError("Этап не принадлежит выбранному проекту", 422)
        project_id = int(stage["project_id"])
    project = None
    if project_id is not None:
        project = conn.execute(
            "SELECT * FROM projects WHERE id=? AND room_id=?", (project_id, room_id)
        ).fetchone()
        if not project:
            raise RoomError("Проект не найден в этой комнате", 404)
    return task, project, stage if stage_id is not None else None


def _reserve_project_scope(conn, project, stage, amount_kop: int) -> None:
    if stage is not None:
        cur = conn.execute(
            """UPDATE stages SET budget_reserved_kop=budget_reserved_kop+?
               WHERE id=? AND room_id=? AND
               budget_reserved_kop+budget_spent_kop+?<=budget_kop""",
            (amount_kop, stage["id"], stage["room_id"], amount_kop),
        )
        if cur.rowcount != 1:
            raise RoomError("Недостаточно свободного бюджета этапа", 409)
    elif project is not None:
        cur = conn.execute(
            """UPDATE projects SET budget_reserved_kop=budget_reserved_kop+?
               WHERE id=? AND room_id=?
                 AND COALESCE((SELECT SUM(s.budget_kop) FROM stages s
                               WHERE s.project_id=projects.id AND s.room_id=projects.room_id),0)
                     +budget_reserved_kop+budget_spent_kop+?<=budget_kop""",
            (amount_kop, project["id"], project["room_id"], amount_kop),
        )
        if cur.rowcount != 1:
            raise RoomError("Недостаточно свободного прямого бюджета проекта", 409)


def _release_project_scope(conn, project, stage, amount: int, actual_spent: int) -> None:
    if stage is not None:
        cur = conn.execute(
            """UPDATE stages SET budget_reserved_kop=budget_reserved_kop-?,
               budget_spent_kop=budget_spent_kop+?
               WHERE id=? AND budget_reserved_kop>=?
                 AND budget_reserved_kop-?+budget_spent_kop+?<=budget_kop""",
            (amount, actual_spent, stage["id"], amount, amount, actual_spent),
        )
        if cur.rowcount != 1:
            raise RoomError("Фактическая выплата превышает остаток бюджета этапа", 409)
    elif project is not None:
        cur = conn.execute(
            """UPDATE projects SET budget_reserved_kop=budget_reserved_kop-?,
               budget_spent_kop=budget_spent_kop+?
               WHERE id=? AND room_id=? AND budget_reserved_kop>=?
                 AND COALESCE((SELECT SUM(s.budget_kop) FROM stages s
                               WHERE s.project_id=projects.id AND s.room_id=projects.room_id),0)
                     +budget_reserved_kop-?+budget_spent_kop+?<=budget_kop""",
            (amount, actual_spent, project["id"], project["room_id"],
             amount, amount, actual_spent),
        )
        if cur.rowcount != 1:
            raise RoomError("Фактическая выплата превышает остаток прямого бюджета проекта", 409)


def reserve_task(conn: sqlite3.Connection, room_id: int, task_id: int,
                 amount_kop: int, actor_id: int | None) -> None:
    if amount_kop < 0:
        raise RoomError("Некорректная сумма резерва", 422)
    task, project, stage = _task_scope(conn, room_id, task_id)
    existing = int(task["budget_reserved_kop"] or 0)
    if existing > 0 or (existing == 0 and amount_kop == 0 and task["status"] in ("available", "in_progress", "submitted", "revision")):
        return
    _reserve_project_scope(conn, project, stage, amount_kop)
    cur = conn.execute("""UPDATE rooms SET budget_reserved_kop=budget_reserved_kop+?
        WHERE id=? AND (budget_limit_kop IS NULL OR
        budget_reserved_kop+budget_spent_kop+?<=budget_limit_kop)""",
        (amount_kop, room_id, amount_kop))
    if cur.rowcount != 1:
        raise RoomError("Недостаточно свободного бюджета комнаты для публикации задачи", 409)
    conn.execute("UPDATE tasks SET budget_reserved_kop=? WHERE id=? AND room_id=?",
                 (amount_kop, task_id, room_id))
    conn.execute("""INSERT INTO budget_events(room_id,task_id,actor_id,event_type,amount_kop)
                    VALUES(?,?,?,'reserved',?)""", (room_id, task_id, actor_id, amount_kop))


def release_task(conn: sqlite3.Connection, room_id: int, task_id: int,
                 actor_id: int | None, event_type: str = "released",
                 spent_amount_kop: int | None = None) -> int:
    if event_type not in ("released", "spent"):
        raise RoomError("Недопустимое событие бюджета", 422)
    task, project, stage = _task_scope(conn, room_id, task_id)
    amount = int(task["budget_reserved_kop"] or 0)
    if amount <= 0:
        return 0
    actual_spent = amount if event_type == "spent" and spent_amount_kop is None else (int(spent_amount_kop or 0) if event_type == "spent" else 0)
    if actual_spent < 0:
        raise RoomError("Фактическая выплата не может быть отрицательной", 422)
    cur = conn.execute("""UPDATE tasks SET budget_reserved_kop=0
        WHERE id=? AND room_id=? AND budget_reserved_kop=?""", (task_id, room_id, amount))
    if cur.rowcount != 1:
        raise RoomError("Резерв задачи уже изменён, обнови страницу", 409)
    cur = conn.execute(
        """UPDATE rooms SET budget_reserved_kop=budget_reserved_kop-?,
           budget_spent_kop=budget_spent_kop+?
           WHERE id=? AND budget_reserved_kop>=?
             AND (budget_limit_kop IS NULL OR
                  budget_reserved_kop-?+budget_spent_kop+?<=budget_limit_kop)""",
        (amount, actual_spent, room_id, amount, amount, actual_spent),
    )
    if cur.rowcount != 1:
        raise RoomError("Фактическая выплата превышает остаток бюджета комнаты", 409)
    _release_project_scope(
        conn, project, stage, amount,
        actual_spent if event_type == "spent" else 0,
    )
    conn.execute("""INSERT INTO budget_events(room_id,task_id,actor_id,event_type,amount_kop,details)
                    VALUES(?,?,?,?,?,?)""",
                 (room_id, task_id, actor_id, event_type, actual_spent if event_type == "spent" else amount,
                  f"reserved={amount};actual_spent={actual_spent}" if event_type == "spent" else ""))
    return amount
