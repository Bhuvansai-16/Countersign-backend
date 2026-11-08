import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, Request


def _sig(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def make_token(secret: str, ttl_s: int = 43200) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + ttl_s}).encode()).decode()
    return f"{body}.{_sig(secret, body)}"


def check_token(secret: str, token: str) -> bool:
    body, _, sig = token.partition(".")
    if not body or not hmac.compare_digest(sig, _sig(secret, body)):
        return False
    try:
        return json.loads(base64.urlsafe_b64decode(body))["exp"] > time.time()
    except (ValueError, KeyError):
        return False


def make_shopping_token(secret: str, ttl_s: int = 86400) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": "visitor_" + secrets.token_hex(16),
                                              "exp": int(time.time()) + ttl_s}).encode()).decode()
    # Separate signing context: a shopping cookie must never validate as a seller token.
    return f"{body}.{_sig(secret, 'shopping:' + body)}"


def shopping_identity(secret: str, token: str) -> str | None:
    try:
        body, _, signature = token.partition(".")
        if not body or not hmac.compare_digest(signature, _sig(secret, "shopping:" + body)):
            return None
        data = json.loads(base64.urlsafe_b64decode(body))
        if data["exp"] > time.time() and isinstance(data["sub"], str) and data["sub"].startswith("visitor_"):
            return data["sub"]
    except (ValueError, KeyError, TypeError):
        pass
    return None


def _bearer(header: str | None) -> str:
    return header[7:].strip() if header and header.lower().startswith("bearer ") else ""


def require_seller(request: Request) -> str:
    token = _bearer(request.headers.get("authorization"))
    if not token or not check_token(request.app.state.cs.settings.session_secret, token):
        raise HTTPException(401, "seller login required")
    return "seller"


def agent_for_key(settings, authorization_header: str | None) -> str | None:
    key = _bearer(authorization_header)
    for known, agent_id in settings.mcp_agent_keys.items():
        if key and hmac.compare_digest(key, known):
            return agent_id
    return None
