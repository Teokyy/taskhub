import hashlib
import hmac
import json
import time
import unittest
import sys
from pathlib import Path
from urllib.parse import urlencode

# Allow unittest discovery from the repository root as well as taskhub_release/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import verify_telegram_init_data


class TelegramAuthTests(unittest.TestCase):
    token = "123456:TEST_TOKEN"

    def make_init_data(self, user=None, auth_date=None):
        values = {
            "auth_date": str(auth_date or int(time.time())),
            "user": json.dumps(user or {"id": 12345, "first_name": "Test"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", self.token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(values)

    def test_accepts_valid_payload(self):
        self.assertEqual(
            verify_telegram_init_data(self.make_init_data(), self.token)["id"],
            12345,
        )

    def test_rejects_expired_payload(self):
        old = int(time.time()) - 7200
        self.assertIsNone(verify_telegram_init_data(self.make_init_data(auth_date=old), self.token))

    def test_rejects_future_payload(self):
        future = int(time.time()) + 600
        self.assertIsNone(verify_telegram_init_data(self.make_init_data(auth_date=future), self.token))

    def test_rejects_invalid_signature(self):
        data = self.make_init_data().replace("Test", "Forged")
        self.assertIsNone(verify_telegram_init_data(data, self.token))

    def test_rejects_duplicate_keys(self):
        data = self.make_init_data() + "&auth_date=1"
        self.assertIsNone(verify_telegram_init_data(data, self.token))

    def test_rejects_user_without_integer_id(self):
        data = self.make_init_data(user={"id": "12345"})
        self.assertIsNone(verify_telegram_init_data(data, self.token))


if __name__ == "__main__":
    unittest.main()
