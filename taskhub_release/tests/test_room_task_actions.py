import sqlite3
import unittest

from app.room_task_actions import refuse_task
from app.rooms import RoomError


class RefuseTaskTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE tasks(
                id INTEGER PRIMARY KEY, room_id INTEGER, assignee_id INTEGER,
                status TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE task_events(
                id INTEGER PRIMARY KEY, task_id INTEGER, actor_id INTEGER,
                event_type TEXT, from_status TEXT, to_status TEXT, details TEXT
            );
            INSERT INTO tasks(id,room_id,assignee_id,status) VALUES
                (1,10,7,'in_progress'),
                (2,10,8,'in_progress'),
                (3,11,7,'in_progress'),
                (4,10,7,'revision');
        """)

    def tearDown(self):
        self.conn.close()

    def test_refusal_releases_only_callers_task_and_logs_event(self):
        refuse_task(self.conn, 10, 1, 7)
        task = self.conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
        self.assertEqual(task["status"], "available")
        self.assertIsNone(task["assignee_id"])
        event = self.conn.execute("SELECT * FROM task_events WHERE task_id=1").fetchone()
        self.assertEqual(event["event_type"], "executor_refused")
        self.assertEqual(event["from_status"], "in_progress")
        self.assertEqual(event["to_status"], "available")

    def test_cannot_refuse_another_users_task(self):
        with self.assertRaises(RoomError):
            refuse_task(self.conn, 10, 2, 7)

    def test_cannot_refuse_task_from_another_room(self):
        with self.assertRaises(RoomError):
            refuse_task(self.conn, 11, 3, 7)

    def test_cannot_refuse_after_submission_for_revision(self):
        with self.assertRaises(RoomError):
            refuse_task(self.conn, 10, 4, 7)


if __name__ == "__main__":
    unittest.main()
