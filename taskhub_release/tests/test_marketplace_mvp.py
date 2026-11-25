import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db, main


class MarketplaceMvpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "taskhub.sqlite3"
        db.init_db()
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO users(telegram_id,username,first_name,role) VALUES(?,?,?,?)",
                [
                    (101, "customer", "Customer", "admin"),
                    (202, "executor", "Executor", "helper"),
                    (303, "outsider", "Outsider", "helper"),
                ],
            )
            self.accounts = {
                "customer": dict(conn.execute("SELECT * FROM users WHERE telegram_id=101").fetchone()),
                "executor": dict(conn.execute("SELECT * FROM users WHERE telegram_id=202").fetchone()),
                "outsider": dict(conn.execute("SELECT * FROM users WHERE telegram_id=303").fetchone()),
            }
        self.user_patch = patch.object(main, "user", side_effect=lambda token: self.accounts[token])
        self.notify_patch = patch.object(main, "notify_telegram", new_callable=AsyncMock)
        self.user_patch.start()
        self.notify_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.notify_patch.stop()
        self.user_patch.stop()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def headers(self, account):
        return {"X-Telegram-Init-Data": account}

    def create_task(self):
        tree = self.client.get("/api/categories", headers=self.headers("customer")).json()
        marketing = next(x for x in tree if x["name"] == "Маркетинг")
        smm = next(x for x in marketing["children"] if x["name"] == "SMM")
        response = self.client.post(
            "/api/market/tasks",
            headers=self.headers("customer"),
            json={
                "title": "Prepare SMM plan",
                "description": "Create a one-week content plan",
                "category_id": smm["id"],
                "category": "Маркетинг / SMM",
                "reward_rub": 1500,
                "pricing_model": "fixed",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_category_tree_is_seeded_as_two_levels(self):
        response = self.client.get("/api/categories", headers=self.headers("customer"))
        self.assertEqual(response.status_code, 200)
        tree = response.json()
        self.assertEqual(len(tree), 10)
        self.assertTrue(all(parent["children"] for parent in tree))
        self.assertEqual(tree[-1]["name"], "Другое")
        self.assertEqual(tree[-1]["children"][0]["name"], "Прочее")

    def test_response_accept_chat_completion_and_reviews(self):
        task_id = self.create_task()
        own_response = self.client.post(
            f"/api/market/tasks/{task_id}/responses",
            headers=self.headers("customer"),
            json={"message": "I can do it"},
        )
        self.assertEqual(own_response.status_code, 403)

        response = self.client.post(
            f"/api/market/tasks/{task_id}/responses",
            headers=self.headers("executor"),
            json={"message": "I have relevant experience", "proposed_deadline": "2026-12-01"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response_id = response.json()["id"]

        duplicate = self.client.post(
            f"/api/market/tasks/{task_id}/responses",
            headers=self.headers("executor"),
            json={"message": "Second response"},
        )
        self.assertEqual(duplicate.status_code, 409)

        accepted = self.client.post(
            f"/api/market/responses/{response_id}/accept",
            headers=self.headers("customer"),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        chat_id = accepted.json()["chat_id"]

        forbidden = self.client.get(
            f"/api/market/chats/{chat_id}/messages",
            headers=self.headers("outsider"),
        )
        self.assertEqual(forbidden.status_code, 404)

        sent = self.client.post(
            f"/api/market/chats/{chat_id}/messages",
            headers=self.headers("executor"),
            json={"body": "Work is ready"},
        )
        self.assertEqual(sent.status_code, 200, sent.text)

        completed = self.client.post(
            f"/api/market/tasks/{task_id}/complete",
            headers=self.headers("customer"),
        )
        self.assertEqual(completed.status_code, 200, completed.text)

        review = self.client.post(
            f"/api/market/tasks/{task_id}/reviews",
            headers=self.headers("customer"),
            json={"rating": 5, "text": "Great work"},
        )
        self.assertEqual(review.status_code, 200, review.text)

        duplicate_review = self.client.post(
            f"/api/market/tasks/{task_id}/reviews",
            headers=self.headers("customer"),
            json={"rating": 4, "text": "Second review"},
        )
        self.assertEqual(duplicate_review.status_code, 409)

    def test_task_edit_history_and_reward_lock_after_selection(self):
        task_id = self.create_task()
        edited = self.client.put(
            f"/api/market/tasks/{task_id}",
            headers=self.headers("customer"),
            json={
                "title": "Updated SMM plan",
                "description": "Updated details",
                "category_id": self.client.get("/api/categories", headers=self.headers("customer")).json()[0]["children"][0]["id"],
                "category": "Маркетинг / SMM",
                "reward_rub": 1600,
                "pricing_model": "fixed",
            },
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        with db.connect() as conn:
            history_count = conn.execute("SELECT COUNT(*) n FROM task_history WHERE task_id=?", (task_id,)).fetchone()["n"]
        self.assertGreater(history_count, 0)


    def test_post_selection_edit_allows_description_but_locks_title_and_reward(self):
        task_id = self.create_task()
        response = self.client.post(
            f"/api/market/tasks/{task_id}/responses",
            headers=self.headers("executor"),
            json={"message": "I can do it"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        accepted = self.client.post(
            f"/api/market/responses/{response.json()['id']}/accept",
            headers=self.headers("customer"),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        tree = self.client.get("/api/categories", headers=self.headers("customer")).json()
        smm_id = next(x["id"] for p in tree if p["name"] == "Маркетинг" for x in p["children"] if x["name"] == "SMM")
        base = {
            "title": "Prepare SMM plan",
            "description": "Clarified requirements",
            "category_id": smm_id,
            "category": "Маркетинг / SMM",
            "reward_rub": 1500,
            "pricing_model": "fixed",
            "deadline": "2026-12-10",
        }
        edited = self.client.put(
            f"/api/market/tasks/{task_id}",
            headers=self.headers("customer"),
            json=base,
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        changed_reward = dict(base, reward_rub=1700)
        locked = self.client.put(
            f"/api/market/tasks/{task_id}",
            headers=self.headers("customer"),
            json=changed_reward,
        )
        self.assertEqual(locked.status_code, 409)
        changed_title = dict(base, title="A different task")
        locked_title = self.client.put(
            f"/api/market/tasks/{task_id}",
            headers=self.headers("customer"),
            json=changed_title,
        )
        self.assertEqual(locked_title.status_code, 409)

    def test_subscriptions_are_limited_to_twenty(self):
        tree = self.client.get("/api/categories", headers=self.headers("customer")).json()
        ids = [child["id"] for parent in tree for child in parent["children"]]
        response = self.client.put(
            "/api/market/subscriptions",
            headers=self.headers("customer"),
            json={"category_ids": ids[:21]},
        )
        self.assertEqual(response.status_code, 422)
        saved = self.client.put(
            "/api/market/subscriptions",
            headers=self.headers("customer"),
            json={"category_ids": ids[:20]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)


if __name__ == "__main__":
    unittest.main()
