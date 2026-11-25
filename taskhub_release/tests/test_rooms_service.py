import sqlite3
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.rooms import (
    RoomError, create_room, list_rooms, require_admin, require_member,
    require_mode, set_work_mode,
)


class RoomServiceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE rooms(
            id INTEGER PRIMARY KEY,name TEXT,owner_id INTEGER REFERENCES users(id),
            payout_day INTEGER DEFAULT 15,moderation_enabled INTEGER DEFAULT 1,archived_at TEXT
        );
        CREATE TABLE room_members(
            id INTEGER PRIMARY KEY,room_id INTEGER REFERENCES rooms(id),
            user_id INTEGER REFERENCES users(id),room_role TEXT,work_mode TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,left_at TEXT,
            UNIQUE(room_id,user_id)
        );
        CREATE TABLE room_payout_settings(
            room_id INTEGER PRIMARY KEY REFERENCES rooms(id),payout_day INTEGER,calendar_region TEXT
        );
        CREATE TABLE tasks(
            id INTEGER PRIMARY KEY,room_id INTEGER,assignee_id INTEGER,created_by INTEGER,status TEXT
        );
        INSERT INTO users VALUES(1),(2);
        """)
        self.room = create_room(self.conn, 1, "Команда A")["id"]
        self.conn.execute(
            "INSERT INTO room_members(room_id,user_id,room_role,work_mode) VALUES(?,2,'member','executor')",
            (self.room,),
        )

    def tearDown(self):
        self.conn.close()

    def test_room_creator_becomes_owner_and_admin(self):
        member = require_admin(self.conn, self.room, 1)
        self.assertEqual(member["room_role"], "owner")

    def test_non_member_cannot_read_room(self):
        with self.assertRaises(RoomError) as err:
            require_member(self.conn, self.room, 99)
        self.assertEqual(err.exception.status_code, 403)

    def test_room_list_is_membership_scoped(self):
        self.assertEqual(len(list_rooms(self.conn, 2)), 1)
        self.assertEqual(list_rooms(self.conn, 2)[0]["id"], self.room)

    def test_executor_cannot_switch_with_in_progress_task(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'in_progress')", (self.room, 2)
        )
        with self.assertRaises(RoomError):
            set_work_mode(self.conn, self.room, 2, "customer")

    def test_submitted_does_not_block_executor_switch(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'submitted')", (self.room, 2)
        )
        result = set_work_mode(self.conn, self.room, 2, "customer")
        self.assertTrue(result["changed"])

    def test_customer_submitted_blocks_switch(self):
        self.conn.execute(
            "UPDATE room_members SET work_mode='customer' WHERE room_id=? AND user_id=2",
            (self.room,),
        )
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,2,'submitted')", (self.room, 1)
        )
        with self.assertRaises(RoomError):
            set_work_mode(self.conn, self.room, 2, "executor")

    def test_revision_blocks_executor_switch(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'revision')", (self.room, 2)
        )
        with self.assertRaises(RoomError):
            set_work_mode(self.conn, self.room, 2, "customer")

    def test_other_room_obligation_does_not_block_switch(self):
        other = create_room(self.conn, 1, "Команда B")["id"]
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'in_progress')", (other, 2)
        )
        result = set_work_mode(self.conn, self.room, 2, "customer")
        self.assertTrue(result["changed"])

    def test_initial_mode_selection_cannot_bypass_existing_executor_obligation(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'in_progress')", (self.room, 2)
        )
        with self.assertRaises(RoomError):
            set_work_mode(self.conn, self.room, 2, "customer")

    def test_initial_mode_selection_cannot_bypass_submitted_customer_task(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'submitted')", (self.room, 2)
        )
        with self.assertRaises(RoomError):
            set_work_mode(self.conn, self.room, 1, "executor")

    def test_initial_executor_mode_is_allowed_to_resolve_assigned_task(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'in_progress')", (self.room, 2)
        )
        result = set_work_mode(self.conn, self.room, 2, "executor")
        self.assertEqual(result["work_mode"], "executor")

    def test_initial_customer_mode_is_allowed_to_review_own_submission(self):
        self.conn.execute(
            "INSERT INTO tasks VALUES(1,?,?,1,'submitted')", (self.room, 2)
        )
        result = set_work_mode(self.conn, self.room, 1, "customer")
        self.assertEqual(result["work_mode"], "customer")

    def test_mode_guard(self):
        with self.assertRaises(RoomError):
            require_mode(self.conn, self.room, 2, "customer")


if __name__ == "__main__":
    unittest.main()
