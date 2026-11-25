"""Explicit task release by an executor, scoped to a room."""
from __future__ import annotations

from os import getenv

from fastapi import APIRouter, Header, HTTPException

from .auth import verify_telegram_init_data
from .db import connect
from .rooms import RoomError, require_mode
from .task_refusal import refuse_task

router = APIRouter()


def _account(init_data: str) -> dict:
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
            """INSERT INTO users(telegram_id,username,first_name,role,photo_url)
               VALUES(?,?,?,?,?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username=excluded.username,first_name=excluded.first_name,role=excluded.role,
                 photo_url=COALESCE(excluded.photo_url,users.photo_url)""",
            (telegram_id, telegram_user.get("username"),
             telegram_user.get("first_name", ""), role, telegram_user.get("photo_url")),
        )
        return dict(conn.execute(
            "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone())


@router.post("/api/rooms/{room_id}/tasks/{task_id}/refuse")
def refuse_room_task(
    room_id: int,
    task_id: int,
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
):
    account = _account(x_telegram_init_data)
    with connect() as conn:
        required = {"rooms", "room_members", "task_events"}
        found = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not required.issubset(found):
            raise HTTPException(503, "Сначала выполните миграцию комнат")
        try:
            require_mode(conn, room_id, account["id"], "executor")
            refuse_task(conn, room_id, task_id, account["id"])
        except RoomError as error:
            raise HTTPException(error.status_code, str(error)) from error
    return {"ok": True, "status": "available"}
