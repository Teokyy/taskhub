import hashlib, hmac, json, time
from urllib.parse import parse_qsl
from os import getenv


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400):
    if not init_data or not bot_token:
        return None
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    auth_date = values.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age_seconds:
                return None
        except ValueError:
            return None
    try:
        return json.loads(values.get("user", "{}"))
    except json.JSONDecodeError:
        return None
