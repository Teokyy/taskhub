import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_PREFIX = "th1"


def _session_key(bot_token: str) -> bytes:
    return hmac.new(b"TaskHubSessionV1", bot_token.encode("utf-8"), hashlib.sha256).digest()


def create_taskhub_session(telegram_id: int, bot_token: str, now: int | None = None) -> str:
    """Create a short-lived, server-signed bearer session after Telegram initData verification."""
    if not bot_token or int(telegram_id) <= 0:
        raise ValueError("A bot token and valid Telegram user ID are required")
    issued_at = int(time.time() if now is None else now)
    payload = json.dumps(
        {"v": 1, "sub": int(telegram_id), "iat": issued_at, "exp": issued_at + SESSION_TTL_SECONDS},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(_session_key(bot_token), encoded.encode("ascii"), hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{SESSION_PREFIX}.{encoded}.{signed}"


def verify_taskhub_session(token: str, bot_token: str, now: int | None = None) -> int | None:
    """Return the Telegram user ID for a valid TaskHub session, otherwise None."""
    if not token or not bot_token:
        return None
    try:
        prefix, encoded, supplied_signature = token.split(".")
        if prefix != SESSION_PREFIX or not encoded or not supplied_signature:
            return None
        expected = hmac.new(_session_key(bot_token), encoded.encode("ascii"), hashlib.sha256).digest()
        padded = supplied_signature + "=" * (-len(supplied_signature) % 4)
        received = base64.urlsafe_b64decode(padded.encode("ascii"))
        if not hmac.compare_digest(expected, received):
            return None
        payload_padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_padded.encode("ascii")))
        current = int(time.time() if now is None else now)
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        telegram_id = int(payload["sub"])
        if payload.get("v") != 1 or telegram_id <= 0:
            return None
        if issued_at > current + 30 or expires_at <= current:
            return None
        if expires_at <= issued_at or expires_at - issued_at > SESSION_TTL_SECONDS:
            return None
        return telegram_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


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
        check = "\\n".join(f"{key}={value}" for key, value in sorted(values.items()))
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
