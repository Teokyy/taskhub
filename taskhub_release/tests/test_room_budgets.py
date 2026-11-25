import sqlite3
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.room_budgets import get_budget, set_budget_limit, reserve_task, release_task
from app.rooms import RoomError


class RoomBudgetTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE rooms(id INTEGER PRIMARY KEY, budget_limit_kop INTEGER,
                budget_reserved_kop INTEGER NOT NULL DEFAULT 0, budget_spent_kop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE projects(id INTEGER PRIMARY KEY, room_id INTEGER,
                budget_kop INTEGER NOT NULL, budget_reserved_kop INTEGER NOT NULL DEFAULT 0,
                budget_spent_kop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE stages(id INTEGER PRIMARY KEY, room_id INTEGER, project_id INTEGER,
                budget_kop INTEGER NOT NULL, budget_reserved_kop INTEGER NOT NULL DEFAULT 0,
                budget_spent_kop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, room_id INTEGER, project_id INTEGER,
                stage_id INTEGER, status TEXT DEFAULT 'available',
                budget_reserved_kop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE budget_events(id INTEGER PRIMARY KEY, room_id INTEGER,
                task_id INTEGER, actor_id INTEGER, event_type TEXT, amount_kop INTEGER,
                details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TRIGGER budget_events_no_update BEFORE UPDATE ON budget_events
                BEGIN SELECT RAISE(ABORT, 'budget_events are immutable'); END;
            CREATE TRIGGER budget_events_no_delete BEFORE DELETE ON budget_events
                BEGIN SELECT RAISE(ABORT, 'budget_events are immutable'); END;
            INSERT INTO rooms(id,budget_limit_kop) VALUES(1,10000),(2,5000);
            INSERT INTO projects(id,room_id,budget_kop) VALUES(20,1,10000);
            INSERT INTO stages(id,room_id,project_id,budget_kop) VALUES(30,1,20,5000);
            INSERT INTO tasks(id,room_id) VALUES(10,1),(11,1),(12,2);
            INSERT INTO tasks(id,room_id,project_id) VALUES(13,1,20),(15,1,20);
            INSERT INTO tasks(id,room_id,project_id,stage_id) VALUES(14,1,20,30),(16,1,20,30);
        """)

    def tearDown(self):
        self.conn.close()

    def test_reservation_reduces_available_budget(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        self.assertEqual(get_budget(self.conn, 1),
                         {"limit_kop": 10000, "reserved_kop": 7000, "spent_kop": 0, "available_kop": 3000})

    def test_reservation_cannot_exceed_limit(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        with self.assertRaises(RoomError):
            reserve_task(self.conn, 1, 11, 4000, 5)
        self.assertEqual(get_budget(self.conn, 1)["reserved_kop"], 7000)

    def test_release_is_idempotent_and_logged_once(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        self.assertEqual(release_task(self.conn, 1, 10, 5), 7000)
        self.assertEqual(release_task(self.conn, 1, 10, 5), 0)
        self.assertEqual(get_budget(self.conn, 1)["reserved_kop"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM budget_events WHERE event_type='released'").fetchone()[0], 1)

    def test_cannot_lower_limit_below_reservations(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        with self.assertRaises(RoomError):
            set_budget_limit(self.conn, 1, 5, 6000)
        self.assertEqual(get_budget(self.conn, 1)["limit_kop"], 10000)

    def test_accepted_task_moves_reserve_to_spent(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        self.assertEqual(release_task(self.conn, 1, 10, 5, "spent"), 7000)
        self.assertEqual(get_budget(self.conn, 1),
                         {"limit_kop": 10000, "reserved_kop": 0,
                          "spent_kop": 7000, "available_kop": 3000})
        with self.assertRaises(RoomError):
            reserve_task(self.conn, 1, 11, 4000, 5)

    def test_cannot_lower_limit_below_spent(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        release_task(self.conn, 1, 10, 5, "spent")
        with self.assertRaises(RoomError):
            set_budget_limit(self.conn, 1, 5, 6000)

    def test_direct_project_tasks_respect_unallocated_project_budget(self):
        reserve_task(self.conn, 1, 13, 4000, 5)
        project = self.conn.execute("SELECT * FROM projects WHERE id=20").fetchone()
        self.assertEqual(project["budget_reserved_kop"], 4000)
        with self.assertRaises(RoomError):
            reserve_task(self.conn, 1, 15, 1500, 5)

    def test_stage_tasks_consume_stage_budget_and_spend(self):
        reserve_task(self.conn, 1, 14, 4000, 5)
        stage = self.conn.execute("SELECT * FROM stages WHERE id=30").fetchone()
        self.assertEqual(stage["budget_reserved_kop"], 4000)
        with self.assertRaises(RoomError):
            reserve_task(self.conn, 1, 16, 2000, 5)
        release_task(self.conn, 1, 14, 5, "spent")
        stage = self.conn.execute("SELECT * FROM stages WHERE id=30").fetchone()
        self.assertEqual(stage["budget_reserved_kop"], 0)
        self.assertEqual(stage["budget_spent_kop"], 4000)

    def test_quality_bonus_is_charged_to_room_budget(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        release_task(self.conn, 1, 10, 5, "spent", 8000)
        self.assertEqual(get_budget(self.conn, 1)["spent_kop"], 8000)

    def test_quality_bonus_cannot_exceed_room_budget(self):
        reserve_task(self.conn, 1, 10, 7000, 5)
        with self.assertRaises(RoomError):
            release_task(self.conn, 1, 10, 5, "spent", 11000)
        self.assertEqual(get_budget(self.conn, 1)["reserved_kop"], 7000)

    def test_actual_project_payout_must_fit_project_cap(self):
        reserve_task(self.conn, 1, 13, 4000, 5)
        with self.assertRaises(RoomError):
            release_task(self.conn, 1, 13, 5, "spent", 6000)
        self.assertEqual(self.conn.execute("SELECT budget_reserved_kop FROM projects WHERE id=20").fetchone()[0], 4000)

    def test_stage_actual_payout_updates_real_spend(self):
        reserve_task(self.conn, 1, 14, 4000, 5)
        release_task(self.conn, 1, 14, 5, "spent", 4500)
        stage = self.conn.execute("SELECT budget_reserved_kop,budget_spent_kop FROM stages WHERE id=30").fetchone()
        self.assertEqual(tuple(stage), (0, 4500))

    def test_stage_must_belong_to_project_in_same_room(self):
        self.conn.execute("INSERT INTO projects(id,room_id,budget_kop) VALUES(21,2,5000)")
        self.conn.execute("INSERT INTO stages(id,room_id,project_id,budget_kop) VALUES(31,2,21,3000)")
        self.conn.execute("UPDATE tasks SET project_id=20,stage_id=31 WHERE id=14")
        with self.assertRaises(RoomError):
            reserve_task(self.conn, 1, 14, 1000, 5)

    def test_budget_history_is_append_only(self):
        self.conn.execute("INSERT INTO budget_events(room_id,event_type,amount_kop) VALUES(1,'reserved',100)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE budget_events SET amount_kop=0 WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM budget_events WHERE id=1")

    def test_budget_is_isolated_between_rooms(self):
        reserve_task(self.conn, 2, 12, 5000, 5)
        self.assertEqual(get_budget(self.conn, 1)["reserved_kop"], 0)
        self.assertEqual(get_budget(self.conn, 2)["available_kop"], 0)


if __name__ == "__main__":
    unittest.main()
