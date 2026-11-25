"""Pure room-scoped task lifecycle operations."""
from .rooms import RoomError


def refuse_task(conn, room_id: int, task_id: int, user_id: int) -> None:
    """Release only the caller's in-progress task and append an immutable event."""
    cur = conn.execute(
        """UPDATE tasks SET status='available',assignee_id=NULL,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND room_id=? AND assignee_id=? AND status='in_progress'""",
        (task_id, room_id, user_id),
    )
    if cur.rowcount != 1:
        raise RoomError(
            "Эту задачу нельзя освободить: она уже изменилась или не назначена тебе",
            409,
        )
    conn.execute(
        """INSERT INTO task_events(task_id,actor_id,event_type,from_status,to_status,details)
           VALUES(?,?,'executor_refused','in_progress','available',
                  'Исполнитель отказался от задачи')""",
        (task_id, user_id),
    )
