"""Room membership and work-mode domain rules.

This module is deliberately independent of FastAPI and the Mini App so its
authorization rules can be tested against temporary SQLite databases.
"""
from __future__ import annotations

import sqlite3
from typing import Any


class RoomError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def require_member(conn: sqlite3.Connection, room_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT rm.*,r.name room_name,r.owner_id
           FROM room_members rm JOIN rooms r ON r.id=rm.room_id
           WHERE rm.room_id=? AND rm.user_id=? AND rm.left_at IS NULL""",
        (room_id, user_id),
    ).fetchone()
    if not row:
        raise RoomError("Нет доступа к этой комнате", 403)
    return row


def require_admin(conn: sqlite3.Connection, room_id: int, user_id: int) -> sqlite3.Row:
    member = require_member(conn, room_id, user_id)
    if member["room_role"] not in ("owner", "admin"):
        raise RoomError("Только владелец или администратор комнаты", 403)
    return member


def list_rooms(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.id,r.name,r.owner_id,rm.room_role,rm.work_mode,rm.joined_at,
                  r.payout_day,r.moderation_enabled
           FROM room_members rm JOIN rooms r ON r.id=rm.room_id
           WHERE rm.user_id=? AND rm.left_at IS NULL AND r.archived_at IS NULL
           ORDER BY CASE rm.room_role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,r.id""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_room(conn: sqlite3.Connection, user_id: int, name: str) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 80:
        raise RoomError("Название комнаты должно содержать от 1 до 80 символов", 422)
    cur = conn.execute(
        "INSERT INTO rooms(name,owner_id,payout_day) VALUES(?,?,15)",
        (clean_name, user_id),
    )
    room_id = cur.lastrowid
    conn.execute(
        """INSERT INTO room_members(room_id,user_id,room_role,work_mode)
           VALUES(?,?,'owner',NULL)""",
        (room_id, user_id),
    )
    conn.execute(
        "INSERT INTO room_payout_settings(room_id,payout_day,calendar_region) VALUES(?,15,'RU')",
        (room_id,),
    )
    return dict(conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone())


def set_work_mode(conn: sqlite3.Connection, room_id: int, user_id: int, new_mode: str) -> dict:
    if new_mode not in ("customer", "executor"):
        raise RoomError("Режим должен быть customer или executor", 422)
    member = require_member(conn, room_id, user_id)
    old_mode = member["work_mode"]
    if old_mode == new_mode:
        return {"room_id": room_id, "work_mode": new_mode, "changed": False}

    # An executor is blocked by work they still owe; a customer is blocked
    # while submitted work awaits their decision. revision blocks both roles.
    if old_mode == "executor" or (old_mode is None and new_mode == "customer"):
        blocked = conn.execute(
            """SELECT id,status FROM tasks
               WHERE room_id=? AND assignee_id=? AND status IN ('in_progress','revision')
               LIMIT 1""",
            (room_id, user_id),
        ).fetchone()
        if blocked:
            raise RoomError(
                f"Нельзя сменить режим: сначала закрой или отклони задачу №{blocked['id']}",
                409,
            )
    if old_mode == "customer" or (old_mode is None and new_mode == "executor"):
        blocked = conn.execute(
            """SELECT id,status FROM tasks
               WHERE room_id=? AND created_by=? AND status IN ('submitted','revision')
               LIMIT 1""",
            (room_id, user_id),
        ).fetchone()
        if blocked:
            raise RoomError(
                f"Нельзя сменить режим: сначала рассмотри задачу №{blocked['id']}",
                409,
            )

    conn.execute(
        "UPDATE room_members SET work_mode=? WHERE room_id=? AND user_id=? AND left_at IS NULL",
        (new_mode, room_id, user_id),
    )
    return {"room_id": room_id, "work_mode": new_mode, "changed": True}


def require_mode(conn: sqlite3.Connection, room_id: int, user_id: int, mode: str) -> sqlite3.Row:
    member = require_member(conn, room_id, user_id)
    if member["work_mode"] != mode:
        label = "заказчика" if mode == "customer" else "исполнителя"
        raise RoomError(f"Для этого действия нужен режим {label}", 403)
    return member
