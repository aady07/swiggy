from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from backend.config import settings
from backend.db import db

router = APIRouter(prefix="/auth", tags=["auth"])

AUTH_SERVER_METADATA_URL = "https://mcp.swiggy.com/.well-known/oauth-authorization-server"
REGISTRATION_ENDPOINT: str | None = None


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


async def _get_registration_endpoint() -> str:
    global REGISTRATION_ENDPOINT
    if REGISTRATION_ENDPOINT:
        return REGISTRATION_ENDPOINT

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(AUTH_SERVER_METADATA_URL)
        resp.raise_for_status()
        data = resp.json()
        REGISTRATION_ENDPOINT = data.get(
            "registration_endpoint", "https://mcp.swiggy.com/auth/register"
        )
        return REGISTRATION_ENDPOINT


async def register_client() -> str:
    endpoint = await _get_registration_endpoint()
    payload = {
        "client_name": "household-grocery-merge-agent",
        "redirect_uris": [settings.oauth_redirect_uri],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(endpoint, json=payload)
        if resp.status_code >= 400:
            # Some deployments use pre-registered public clients; fall back to dynamic id
            return secrets.token_urlsafe(16)
        data = resp.json()
        return data.get("client_id", secrets.token_urlsafe(16))


async def get_valid_access_token() -> str | None:
    token_row = await db.get_token()
    if not token_row:
        return None
    expires_at = token_row.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp <= datetime.now(timezone.utc):
                await db.clear_token()
                return None
        except ValueError:
            pass
    return token_row.get("access_token")


async def require_access_token() -> str:
    token = await get_valid_access_token()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated with Swiggy MCP")
    return token


@router.get("/status")
async def auth_status():
    token_row = await db.get_token()
    if not token_row:
        return {"authenticated": False, "expires_at": None}
    token = await get_valid_access_token()
    return {
        "authenticated": bool(token),
        "expires_at": token_row.get("expires_at"),
    }


@router.get("/login")
async def auth_login():
    client_id = await register_client()
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    await db.save_oauth_pending(state, verifier, client_id)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "mcp:tools",
    }
    url = f"{settings.swiggy_auth_authorize_url}?{urlencode(params)}"
    return RedirectResponse(url)


@router.get("/callback")
async def auth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            f"<h1>Authentication failed</h1><p>{error}</p>"
            "<p><a href='/'>Return to app</a></p>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    pending = await db.pop_oauth_pending(state)
    if not pending:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": pending["code_verifier"],
        "redirect_uri": settings.oauth_redirect_uri,
        "client_id": pending["client_id"],
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(settings.swiggy_auth_token_url, json=payload)
        if resp.status_code >= 400:
            detail = resp.text
            return HTMLResponse(
                f"<h1>Token exchange failed</h1><pre>{detail}</pre>"
                "<p><a href='/auth/login'>Try again</a></p>",
                status_code=400,
            )
        data = resp.json()

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access token in response")

    expires_in = int(data.get("expires_in", 432000))  # default 5 days
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    await db.save_token(access_token, expires_at, pending["client_id"])

    return HTMLResponse(
        "<h1>Swiggy connected!</h1><p>You can close this tab and return to the app.</p>"
        "<script>window.close();</script>"
        "<p><a href='/'>Go to app</a></p>"
    )


async def invalidate_token_on_401() -> None:
    await db.clear_token()
