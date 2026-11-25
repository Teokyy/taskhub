import sqlite3
import tempfile
import unittest
from pathlib import Path
import importlib.util

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_rooms.py"
spec = importlib.util.spec_from_file_location("migrate_to_rooms", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class RoomMigrationTests(unittest.TestCase):
    def make_legacy_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT NOT NULL);
        CREATE TABLE projects(id INTEGER PRIMARY KEY, created_by INTEGER);
        CREATE TABLE stages(id INTEGER PRIMARY KEY, project_id INTEGER);
        CREATE TABLE tours(id INTEGER PRIMARY KEY, stage_id INTEGER);
        CREATE TABLE tasks(id INTEGER PRIMARY KEY, reward_kop INTEGER, status TEXT);
        CREATE TABLE submissions(id INTEGER PRIMARY KEY, task_id INTEGER);
        CREATE TABLE ledger(id INTEGER PRIMARY KEY, user_id INTEGER, amount_kop INTEGER);
        CREATE TABLE audit(id INTEGER PRIMARY KEY);
        CREATE TABLE task_criteria(id INTEGER PRIMARY KEY, task_id INTEGER);
        CREATE TABLE criterion_results(id INTEGER PRIMARY KEY);
        CREATE TABLE disputes(id INTEGER PRIMARY KEY);
        CREATE TABLE payouts(id INTEGER PRIMARY KEY, user_id INTEGER, period_start TEXT,
            period_end TEXT, amount_kop INTEGER);
        INSERT INTO users VALUES(1,'admin'),(2,'helper');
        INSERT INTO projects VALUES(10,1);
        INSERT INTO stages VALUES(11,10);
        INSERT INTO tours VALUES(12,11);
        INSERT INTO tasks VALUES(13,5000,'accepted');
        INSERT INTO submissions VALUES(14,13);
        INSERT INTO ledger VALUES(15,2,5000);
        INSERT INTO audit VALUES(16);
        INSERT INTO task_criteria VALUES(17,13);
        INSERT INTO criterion_results VALUES(18);
        INSERT INTO disputes VALUES(19);
        INSERT INTO payouts VALUES(20,2,'2026-01-01','2026-01-31',5000);
        """)
        return conn

    def test_migration_preserves_rows_and_money_and_assigns_one_room(self):
        conn = self.make_legacy_db()
        before = migration.snapshot(conn)
        outcome = migration.migrate(conn)
        self.assertEqual(outcome["status"], "migrated")
        self.assertEqual(before, migration.snapshot(conn))
        room_id = outcome["legacy_room_id"]
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM room_members").fetchone()[0], 2)
        for table in ("projects","stages","tours","tasks","ledger","payouts"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE room_id=?", (room_id,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT room_role FROM room_members WHERE user_id=1").fetchone()[0], "owner")
        self.assertIsNone(conn.execute("SELECT work_mode FROM room_members WHERE user_id=2").fetchone()[0])

    def test_migration_is_idempotent(self):
        conn = self.make_legacy_db()
        first = migration.migrate(conn)
        second = migration.migrate(conn)
        self.assertEqual(first["legacy_room_id"], second["legacy_room_id"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 1)

    def test_empty_user_database_is_refused(self):
        conn = self.make_legacy_db()
        conn.execute("DELETE FROM users")
        with self.assertRaises(RuntimeError):
            migration.migrate(conn)


if __name__ == "__main__":
    unittest.main()
