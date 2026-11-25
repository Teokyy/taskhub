import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main
import importlib.util

MIGRATION_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_rooms.py"
SPEC = importlib.util.spec_from_file_location("room_api_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class RoomApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "taskhub.sqlite3"
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO users(telegram_id,username,first_name,role) VALUES(?,?,?,?)",
                [(101, "owner", "Owner", "admin"), (202, "worker", "Worker", "helper")],
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            MIGRATION.migrate(conn)
        with db.connect() as conn:
            self.accounts = {
                "owner-token": dict(conn.execute("SELECT * FROM users WHERE telegram_id=101").fetchone()),
                "worker-token": dict(conn.execute("SELECT * FROM users WHERE telegram_id=202").fetchone()),
            }
        self.user_patch = patch.object(main, "user", side_effect=lambda token: self.accounts[token])
        self.user_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.user_patch.stop()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def headers(self, token):
        return {"X-Telegram-Init-Data": token}

    def test_room_project_stage_task_acceptance_and_ledger(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")

        rooms = self.client.get("/api/rooms", headers=owner)
        self.assertEqual(rooms.status_code, 200)
        room_id = rooms.json()[0]["id"]

        self.assertEqual(
            self.client.put(f"/api/rooms/{room_id}/mode", headers=owner,
                            json={"mode": "customer"}).status_code, 200
        )
        self.assertEqual(
            self.client.put(f"/api/rooms/{room_id}/mode", headers=worker,
                            json={"mode": "executor"}).status_code, 200
        )

        project = self.client.post(
            f"/api/rooms/{room_id}/projects", headers=owner,
            json={"title": "Project Alpha", "description": "", "budget_rub": 100},
        )
        self.assertEqual(project.status_code, 200, project.text)
        project_id = project.json()["id"]

        stage = self.client.post(
            f"/api/rooms/{room_id}/projects/{project_id}/stages", headers=owner,
            json={"title": "Stage One", "description": "", "budget_rub": 60},
        )
        self.assertEqual(stage.status_code, 200, stage.text)
        stage_id = stage.json()["id"]

        task = self.client.post(
            f"/api/rooms/{room_id}/tasks", headers=owner,
            json={"title": "Do the work", "description": "Deliver it",
                  "category": "Research", "reward_rub": 50, "pricing_model": "fixed",
                  "difficulty": 1, "urgency": 1, "project_id": project_id,
                  "stage_id": stage_id},
        )
        self.assertEqual(task.status_code, 200, task.text)
        task_id = task.json()["id"]
        self.assertEqual(task.json()["status"], "pending")
        self.assertEqual(task.json()["budget_reserved_kop"], 0)

        queue = self.client.get(f"/api/rooms/{room_id}/moderation", headers=owner)
        self.assertEqual([row["id"] for row in queue.json()], [task_id])
        approved = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        with db.connect() as conn:
            room = conn.execute("SELECT budget_reserved_kop FROM rooms WHERE id=?", (room_id,)).fetchone()
            stage_budget = conn.execute("SELECT budget_reserved_kop FROM stages WHERE id=?", (stage_id,)).fetchone()
        self.assertEqual(room["budget_reserved_kop"], 5000)
        self.assertEqual(stage_budget["budget_reserved_kop"], 5000)

        claimed = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/claim", headers=worker
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        submitted = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/submit", headers=worker,
            json={"body": "Completed work"},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)

        review_queue = self.client.get(f"/api/rooms/{room_id}/review", headers=owner)
        self.assertEqual([row["id"] for row in review_queue.json()], [task_id])
        accepted = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/review", headers=owner,
            json={"decision": "accept", "quality": "excellent",
                  "comment": "Above expectations"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)

        duplicate = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/review", headers=owner,
            json={"decision": "accept", "quality": "excellent",
                  "comment": "Duplicate acceptance"},
        )
        self.assertEqual(duplicate.status_code, 409)

        finance = self.client.get(f"/api/rooms/{room_id}/ledger", headers=worker)
        self.assertEqual(finance.status_code, 200)
        self.assertEqual(finance.json()["balance_kop"], 5500)
        self.assertEqual(len(finance.json()["entries"]), 1)
        with db.connect() as conn:
            room = conn.execute(
                "SELECT budget_reserved_kop,budget_spent_kop FROM rooms WHERE id=?", (room_id,)
            ).fetchone()
            stage_budget = conn.execute(
                "SELECT budget_reserved_kop,budget_spent_kop FROM stages WHERE id=?", (stage_id,)
            ).fetchone()
            events = conn.execute(
                "SELECT event_type,amount_kop FROM budget_events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        self.assertEqual(tuple(room), (0, 5500))
        self.assertEqual(tuple(stage_budget), (0, 5500))
        self.assertEqual([(e["event_type"], e["amount_kop"]) for e in events],
                         [("reserved", 5000), ("spent", 5500)])

    def test_cross_room_and_legacy_api_are_blocked(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        rooms = self.client.get("/api/rooms", headers=owner).json()
        room_id = rooms[0]["id"]
        second_room = self.client.post(
            "/api/rooms", headers=worker, json={"name": "Private room"}
        )
        self.assertEqual(second_room.status_code, 200)
        other_id = second_room.json()["id"]
        self.assertEqual(self.client.get(
            f"/api/rooms/{other_id}/budget", headers=owner
        ).status_code, 403)
        self.assertEqual(self.client.get("/api/tasks", headers=owner).status_code, 410)
        self.assertEqual(self.client.get(
            f"/api/rooms/{other_id}/team", headers=owner
        ).status_code, 403)

    def test_stage_budget_overrun_does_not_publish_or_reserve(self):
        owner = self.headers("owner-token")
        rooms = self.client.get("/api/rooms", headers=owner).json()
        room_id = rooms[0]["id"]
        self.client.put(f"/api/rooms/{room_id}/mode", headers=owner,
                        json={"mode": "customer"})
        project = self.client.post(
            f"/api/rooms/{room_id}/projects", headers=owner,
            json={"title": "Small project", "description": "", "budget_rub": 100},
        ).json()
        stage = self.client.post(
            f"/api/rooms/{room_id}/projects/{project['id']}/stages", headers=owner,
            json={"title": "Small stage", "description": "", "budget_rub": 10},
        ).json()
        task = self.client.post(
            f"/api/rooms/{room_id}/tasks", headers=owner,
            json={"title": "Too expensive", "reward_rub": 20, "pricing_model": "fixed",
                  "project_id": project["id"], "stage_id": stage["id"]},
        )
        self.assertEqual(task.status_code, 200, task.text)
        task_id = task.json()["id"]
        approval = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        )
        self.assertEqual(approval.status_code, 409)
        with db.connect() as conn:
            row = conn.execute("SELECT status,budget_reserved_kop FROM tasks WHERE id=?", (task_id,)).fetchone()
            stage_row = conn.execute(
                "SELECT budget_reserved_kop,budget_spent_kop FROM stages WHERE id=?", (stage["id"],)
            ).fetchone()
        self.assertEqual(tuple(row), ("pending", 0))
        self.assertEqual(tuple(stage_row), (0, 0))


if __name__ == "__main__":
    unittest.main()
