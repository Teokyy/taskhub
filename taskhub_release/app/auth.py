import hashlib, hmac, json, time
from urllib.parse import parse_qsl

def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400):
    if not init_data or not bot_token: return None
    values=dict(parse_qsl(init_data, keep_blank_values=True)); received=values.pop("hash", None)
    if not received: return None
    check="\n".join(f"{k}={v}" for k,v in sorted(values.items()))
    secret=hmac.new(b"WebAppData",bot_token.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): return None
    try:
        if time.time()-int(values.get("auth_date",0))>max_age_seconds: return None
        return json.loads(values.get("user","{}"))
    except (ValueError,json.JSONDecodeError): return None
