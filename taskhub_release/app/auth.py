import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 3600,
    clock_skew_seconds: int = 30,
):
    """Verify Telegram Mini App initData and reject stale or future-dated payloads."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        if len({key for key, _ in pairs}) != len(pairs):
            return None
        values = dict(pairs)
        received_hash = values.pop("hash", None)
        if not received_hash:
            return None
        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        auth_date = int(values.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + clock_skew_seconds:
            return None
        if now - auth_date > max_age_seconds:
            return None
        user = json.loads(values.get("user", "{}"))
        if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
            return None
        return user
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
