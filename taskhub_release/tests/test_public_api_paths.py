"""Regression coverage for the public API and retired global routes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db, main


PUBLIC_GET_PATHS = (
    "/api/me",
    "/api/health",
    "/api/categories",
    "/api/categories?include_hidden=true",
    "/api/onboarding",
    "/api/market/profile",
    "/api/market/ledger",
    "/api/market/tasks?limit=50&offset=0",
    "/api/market/tasks?status=mine",
    "/api/market/subscriptions",
    "/api/market/notification-preferences",
    "/api/market/chats",
    "/api/category-requests?status=pending",
    "/api/moderation/disputes",
    "/api/rooms",
)
LEGACY_PATHS = (
    ("GET", "/api/tasks?status=all"),
    ("GET", "/api/moderation"),
    ("GET", "/api/team"),
    ("GET", "/api/review"),
    ("GET", "/api/ledger"),
    ("POST", "/api/tasks/1/claim"),
)


class PublicApiPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "taskhub.sqlite3"
        db.init_db()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users(telegram_id,username,first_name,role) VALUES(?,?,?,?)",
                (101, "admin", "Admin", "admin"),
            )
            self.account = dict(
                conn.execute("SELECT * FROM users WHERE telegram_id=101").fetchone()
            )
        self.user_patch = patch.object(main, "user", side_effect=lambda token: self.account)
        self.notify_patch = patch.object(main, "notify_telegram", new_callable=AsyncMock)
        self.user_patch.start()
        self.notify_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.notify_patch.stop()
        self.user_patch.stop()
        db.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def headers(self):
        return {"X-Telegram-Init-Data": "admin"}

    def migrate(self):
        with db.connect() as conn:
            main.ensure_rooms_schema(conn)

    def test_public_path_matching_is_boundary_safe(self):
        self.assertTrue(main.is_public_api_path("/api/categories"))
        self.assertTrue(main.is_public_api_path("/api/categories/3"))
        self.assertTrue(main.is_public_api_path("/api/market/tasks/7"))
        self.assertTrue(main.is_public_api_path("/api/rooms/5/tasks"))
        self.assertTrue(main.is_public_api_path("/api/rooms"))
        self.assertFalse(main.is_public_api_path("/api/tasks"))
        self.assertFalse(main.is_public_api_path("/api/moderation"))
        self.assertFalse(main.is_public_api_path("/api/categoriesX"))
        self.assertFalse(main.is_public_api_path("/api/roomsX"))

    def test_legacy_routes_are_not_blocked_before_migration(self):
        for method, path in LEGACY_PATHS:
            with self.subTest(path=path):
                response = self.client.request(method, path, headers=self.headers())
                self.assertNotEqual(response.status_code, 410, path)

    def test_frontend_public_get_routes_work_after_migration(self):
        self.migrate()
        for path in PUBLIC_GET_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(
                    response.status_code, 200,
                    f"{path}: {response.status_code} {response.text[:200]}",
                )

    def test_category_tree_and_market_feed_have_expected_shapes(self):
        self.migrate()
        tree = self.client.get("/api/categories", headers=self.headers())
        self.assertEqual(tree.status_code, 200, tree.text)
        self.assertIsInstance(tree.json(), list)
        self.assertTrue(tree.json())
        self.assertIn("name", tree.json()[0])
        self.assertIsInstance(tree.json()[0].get("children"), list)

        feed = self.client.get(
            "/api/market/tasks?limit=50&offset=0", headers=self.headers()
        )
        self.assertEqual(feed.status_code, 200, feed.text)
        self.assertIsInstance(feed.json(), list)

    def test_legacy_routes_return_410_after_migration(self):
        self.migrate()
        for method, path in LEGACY_PATHS:
            with self.subTest(path=path):
                response = self.client.request(method, path, headers=self.headers())
                self.assertEqual(response.status_code, 410, path)
                self.assertEqual(response.json()["path"], path.split("?")[0])


if __name__ == "__main__":
    unittest.main()
