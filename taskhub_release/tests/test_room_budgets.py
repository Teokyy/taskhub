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
            CREATE TABLE tasks(id INTEGER PRIMARY KEY, room_id INTEGER,
                budget_reserved_kop INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE budget_events(id INTEGER PRIMARY KEY, room_id INTEGER,
                task_id INTEGER, actor_id INTEGER, event_type TEXT, amount_kop INTEGER,
                details TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO rooms(id,budget_limit_kop) VALUES(1,10000),(2,5000);
            INSERT INTO tasks(id,room_id) VALUES(10,1),(11,1),(12,2);
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

    def test_budget_is_isolated_between_rooms(self):
        reserve_task(self.conn, 2, 12, 5000, 5)
        self.assertEqual(get_budget(self.conn, 1)["reserved_kop"], 0)
        self.assertEqual(get_budget(self.conn, 2)["available_kop"], 0)


if __name__ == "__main__":
    unittest.main()
