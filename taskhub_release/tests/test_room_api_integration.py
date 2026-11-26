import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main, room_task_actions
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
                [(101, "owner", "Owner", "admin"), (202, "worker", "Worker", "helper"),
                 (303, "reviewer", "Reviewer", "helper")],
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            MIGRATION.migrate(conn)
        with db.connect() as conn:
            self.accounts = {
                "owner-token": dict(conn.execute("SELECT * FROM users WHERE telegram_id=101").fetchone()),
                "worker-token": dict(conn.execute("SELECT * FROM users WHERE telegram_id=202").fetchone()),
                "reviewer-token": dict(conn.execute("SELECT * FROM users WHERE telegram_id=303").fetchone()),
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

    def test_global_categories_api_survives_room_migration(self):
        # The marketplace category tree is account-global, not room-scoped.
        # It must remain available after the legacy room migration creates
        # the rooms table (which activates the legacy API guard).
        response = self.client.get("/api/categories", headers=self.headers("owner-token"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json(), list)

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


    def test_invite_join_limit_and_duplicate_membership(self):
        owner = self.headers("owner-token")
        worker = self.headers("reviewer-token")
        room_id = self.client.post("/api/rooms", headers=owner, json={"name": "Invite test room"}).json()["id"]
        invite = self.client.post(
            f"/api/rooms/{room_id}/invites", headers=owner,
            json={"expires_in_days": 7, "max_uses": 1},
        )
        self.assertEqual(invite.status_code, 200, invite.text)
        code = invite.json()["code"]
        joined = self.client.post("/api/rooms/join", headers=worker, json={"code": code})
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertEqual(joined.json()["room_id"], room_id)
        self.assertEqual(joined.json()["work_mode"], None)
        duplicate = self.client.post("/api/rooms/join", headers=worker, json={"code": code})
        self.assertEqual(duplicate.status_code, 410)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT uses_count FROM room_invites WHERE id=?", (invite.json()["id"],)
            ).fetchone()
        self.assertEqual(row["uses_count"], 1)

    def test_invite_revoke_and_invalid_code(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        invite = self.client.post(
            f"/api/rooms/{room_id}/invites", headers=owner,
            json={"expires_in_days": 7, "max_uses": 10},
        ).json()
        revoked = self.client.delete(
            f"/api/rooms/{room_id}/invites/{invite['id']}", headers=owner
        )
        self.assertEqual(revoked.status_code, 200)
        denied = self.client.post(
            "/api/rooms/join", headers=worker, json={"code": invite["code"]}
        )
        self.assertEqual(denied.status_code, 404)
        invalid = self.client.post(
            "/api/rooms/join", headers=worker, json={"code": "x" * 32}
        )
        self.assertEqual(invalid.status_code, 404)

    def test_member_role_permissions_and_removal(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        reviewer = self.headers("reviewer-token")
        room_id = self.client.post("/api/rooms", headers=owner, json={"name": "Role test room"}).json()["id"]
        joined = self.client.post(
            f"/api/rooms/{room_id}/invites", headers=owner,
            json={"expires_in_days": 7, "max_uses": 10},
        ).json()
        # Add reviewer through an invitation.
        self.assertEqual(self.client.post(
            "/api/rooms/join", headers=reviewer, json={"code": joined["code"]}
        ).status_code, 200)
        reviewer_id = self.accounts["reviewer-token"]["id"]
        promoted = self.client.patch(
            f"/api/rooms/{room_id}/members/{reviewer_id}", headers=owner,
            json={"room_role": "admin"},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        demoted = self.client.patch(
            f"/api/rooms/{room_id}/members/{reviewer_id}", headers=worker,
            json={"room_role": "member"},
        )
        self.assertEqual(demoted.status_code, 403)
        removed = self.client.delete(
            f"/api/rooms/{room_id}/members/{reviewer_id}", headers=owner
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        with db.connect() as conn:
            member = conn.execute(
                "SELECT left_at FROM room_members WHERE room_id=? AND user_id=?",
                (room_id, reviewer_id),
            ).fetchone()
        self.assertIsNotNone(member["left_at"])

    def test_member_with_active_task_cannot_be_removed(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO tasks(title,room_id,created_by,assignee_id,status) VALUES(?,?,?,?,?)",
                ("Active obligation", room_id, self.accounts["owner-token"]["id"],
                 self.accounts["worker-token"]["id"], "in_progress"),
            )
        denied = self.client.delete(
            f"/api/rooms/{room_id}/members/{self.accounts['worker-token']['id']}",
            headers=owner,
        )
        self.assertEqual(denied.status_code, 409)


    def test_marketplace_and_onboarding_routes_survive_legacy_api_guard(self):
        owner = self.headers("owner-token")
        checks = [
            ("/api/market/profile", 200),
            ("/api/market/tasks?limit=5&offset=0", 200),
            ("/api/categories", 200),
            ("/api/onboarding", 200),
            ("/api/market/subscriptions", 200),
        ]
        for path, expected_status in checks:
            with self.subTest(path=path):
                response = self.client.get(path, headers=owner)
                self.assertEqual(response.status_code, expected_status, response.text)
                self.assertNotIn("Этот адрес относится к старому API", response.text)

    def test_active_global_api_endpoints_are_not_blocked_as_legacy(self):
        owner = self.headers("owner-token")
        checks = [
            ("GET", "/api/presence", 405),
            ("POST", "/api/presence", 200),
            ("GET", "/api/category-requests?status=pending", 200),
            ("GET", "/api/moderation/disputes", 200),
        ]
        for method, path, expected_status in checks:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers=owner)
                self.assertEqual(response.status_code, expected_status, response.text)
                self.assertNotIn("Этот адрес относится к старому API", response.text)

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


    def test_only_task_creator_can_review_submission(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        reviewer = self.headers("reviewer-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        for token, mode in (("owner-token", "customer"), ("worker-token", "executor"),
                            ("reviewer-token", "customer")):
            self.assertEqual(self.client.put(
                f"/api/rooms/{room_id}/mode", headers=self.headers(token),
                json={"mode": mode},
            ).status_code, 200)
        task = self.client.post(
            f"/api/rooms/{room_id}/tasks", headers=owner,
            json={"title": "Creator-only review", "reward_rub": 10,
                  "pricing_model": "fixed"},
        ).json()
        task_id = task["id"]
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/claim", headers=worker
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/submit", headers=worker,
            json={"body": "Ready"},
        ).status_code, 200)
        self.assertEqual(self.client.get(
            f"/api/rooms/{room_id}/review", headers=reviewer
        ).json(), [])
        denied = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/review", headers=reviewer,
            json={"decision": "accept", "quality": "good", "comment": ""},
        )
        self.assertEqual(denied.status_code, 403)

    def test_quality_bonus_over_room_limit_rolls_back_acceptance(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        self.client.put(f"/api/rooms/{room_id}/mode", headers=owner,
                        json={"mode": "customer"})
        self.client.put(f"/api/rooms/{room_id}/mode", headers=worker,
                        json={"mode": "executor"})
        self.assertEqual(self.client.put(
            f"/api/rooms/{room_id}/budget", headers=owner,
            json={"limit_rub": 50},
        ).status_code, 200)
        task = self.client.post(
            f"/api/rooms/{room_id}/tasks", headers=owner,
            json={"title": "Bonus above cap", "reward_rub": 50,
                  "pricing_model": "fixed"},
        ).json()
        task_id = task["id"]
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/claim", headers=worker
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/submit", headers=worker,
            json={"body": "Done"},
        ).status_code, 200)
        rejected_by_budget = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/review", headers=owner,
            json={"decision": "accept", "quality": "outstanding",
                  "comment": "Excellent"},
        )
        self.assertEqual(rejected_by_budget.status_code, 409)
        with db.connect() as conn:
            task_row = conn.execute(
                "SELECT status,budget_reserved_kop FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            room = conn.execute(
                "SELECT budget_reserved_kop,budget_spent_kop FROM rooms WHERE id=?", (room_id,)
            ).fetchone()
            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM ledger WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        self.assertEqual(tuple(task_row), ("submitted", 5000))
        self.assertEqual(tuple(room), (5000, 0))
        self.assertEqual(ledger_count, 0)

    def test_direct_project_tasks_cannot_spend_stage_allocations(self):
        owner = self.headers("owner-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        self.client.put(f"/api/rooms/{room_id}/mode", headers=owner,
                        json={"mode": "customer"})
        project = self.client.post(
            f"/api/rooms/{room_id}/projects", headers=owner,
            json={"title": "Ring-fenced project", "description": "", "budget_rub": 100},
        ).json()
        self.client.post(
            f"/api/rooms/{room_id}/projects/{project['id']}/stages", headers=owner,
            json={"title": "Allocated stage", "description": "", "budget_rub": 60},
        )
        task_ids = []
        for title in ("Direct task one", "Direct task two"):
            task = self.client.post(
                f"/api/rooms/{room_id}/tasks", headers=owner,
                json={"title": title, "reward_rub": 40, "pricing_model": "fixed",
                      "project_id": project["id"]},
            )
            self.assertEqual(task.status_code, 200, task.text)
            task_ids.append(task.json()["id"])
        first = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_ids[0]}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        )
        second = self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_ids[1]}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409)
        with db.connect() as conn:
            project_row = conn.execute(
                "SELECT budget_reserved_kop,budget_spent_kop FROM projects WHERE id=?",
                (project["id"],),
            ).fetchone()
            pending = conn.execute(
                "SELECT status,budget_reserved_kop FROM tasks WHERE id=?", (task_ids[1],)
            ).fetchone()
        self.assertEqual(tuple(project_row), (4000, 0))
        self.assertEqual(tuple(pending), ("pending", 0))



    def test_executor_refusal_returns_task_without_releasing_budget(self):
        owner = self.headers("owner-token")
        worker = self.headers("worker-token")
        room_id = self.client.get("/api/rooms", headers=owner).json()[0]["id"]
        self.client.put(f"/api/rooms/{room_id}/mode", headers=owner,
                        json={"mode": "customer"})
        self.client.put(f"/api/rooms/{room_id}/mode", headers=worker,
                        json={"mode": "executor"})
        task = self.client.post(
            f"/api/rooms/{room_id}/tasks", headers=owner,
            json={"title": "Refusable task", "reward_rub": 10,
                  "pricing_model": "fixed"},
        ).json()
        task_id = task["id"]
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/moderate", headers=owner,
            json={"decision": "approve", "comment": ""},
        ).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/rooms/{room_id}/tasks/{task_id}/claim", headers=worker
        ).status_code, 200)
        with patch.object(room_task_actions, "_account",
                          return_value=self.accounts["worker-token"]):
            refused = self.client.post(
                f"/api/rooms/{room_id}/tasks/{task_id}/refuse", headers=worker
            )
        self.assertEqual(refused.status_code, 200, refused.text)
        with db.connect() as conn:
            task_row = conn.execute(
                "SELECT status,assignee_id,budget_reserved_kop FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            room = conn.execute(
                "SELECT budget_reserved_kop FROM rooms WHERE id=?", (room_id,)
            ).fetchone()
            event = conn.execute(
                "SELECT event_type FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        self.assertEqual(tuple(task_row), ("available", None, 1000))
        self.assertEqual(room["budget_reserved_kop"], 1000)
        self.assertEqual(event["event_type"], "executor_refused")



if __name__ == "__main__":
    unittest.main()
