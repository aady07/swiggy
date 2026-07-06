from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Header, HTTPException

from backend.config import settings
from backend.models import AdminLoginRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])

SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def _session_secret() -> bytes:
    secret = settings.admin_session_secret.strip() or settings.admin_password
    return secret.encode("utf-8")


def create_session_token(username: str) -> str:
    issued = int(time.time())
    payload = f"{username}:{issued}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        username, issued = payload.split(":", 1)
    except ValueError:
        return None
    if username != settings.admin_username:
        return None
    if time.time() - int(issued) > SESSION_TTL_SECONDS:
        return None
    return username


def verify_admin_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username.strip(), settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def require_admin(authorization: str | None = Header(None)) -> str:
    user = verify_session_token(extract_bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.post("/login")
async def admin_login(body: AdminLoginRequest):
    if not verify_admin_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session_token(body.username.strip())
    return {"ok": True, "token": token, "username": settings.admin_username}


@router.get("/session")
async def admin_session(authorization: str | None = Header(None)):
    user = verify_session_token(extract_bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"ok": True, "username": user}


@router.post("/logout")
async def admin_logout():
    return {"ok": True}
