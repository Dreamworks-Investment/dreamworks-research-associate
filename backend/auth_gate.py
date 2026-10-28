from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("utf-8"))


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class AuthUser:
    email: str
    role: str = "admin"


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: Dict[str, Any]


class MeResponse(BaseModel):
    email: str
    role: str


AUTH_ENABLED = _truthy(os.getenv("MGC_AUTH_ENABLED"), default=True)
AUTH_EMAIL = (os.getenv("MGC_AUTH_EMAIL") or "").strip().lower()
AUTH_PASSWORD = os.getenv("MGC_AUTH_PASSWORD") or ""
AUTH_SECRET_KEY = os.getenv("MGC_AUTH_SECRET_KEY") or ""
AUTH_TOKEN_EXPIRE_MINUTES = int(os.getenv("MGC_AUTH_TOKEN_EXPIRE_MINUTES") or "30")

PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/healthz",
    "/auth/login",
}

PUBLIC_PATH_PREFIXES = (
    "/static/",
)


def _ensure_auth_config() -> None:
    if not AUTH_ENABLED:
        return

    missing = []
    if not AUTH_EMAIL:
        missing.append("MGC_AUTH_EMAIL")
    if not AUTH_PASSWORD:
        missing.append("MGC_AUTH_PASSWORD")
    if not AUTH_SECRET_KEY:
        missing.append("MGC_AUTH_SECRET_KEY")

    if missing:
        raise RuntimeError("Missing auth env vars: " + ", ".join(missing))


def _sign(message: str) -> str:
    digest = hmac.new(
        AUTH_SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(digest)


def create_access_token(user: AuthUser) -> str:
    now = int(time.time())
    exp = now + AUTH_TOKEN_EXPIRE_MINUTES * 60

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user.email,
        "role": user.role,
        "iat": now,
        "exp": exp,
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}"
    signature = _sign(signing_input)

    return f"{signing_input}.{signature}"


def decode_access_token(token: str) -> AuthUser:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("bad token format")

        signing_input = f"{parts[0]}.{parts[1]}"
        expected_signature = _sign(signing_input)

        if not hmac.compare_digest(expected_signature, parts[2]):
            raise ValueError("bad signature")

        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        exp = int(payload.get("exp") or 0)

        if exp < int(time.time()):
            raise ValueError("token expired")

        email = (payload.get("sub") or "").strip().lower()
        role = payload.get("role") or "admin"

        if not email:
            raise ValueError("missing subject")

        return AuthUser(email=email, role=role)

    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def authenticate(email: str, password: str) -> Optional[AuthUser]:
    email = (email or "").strip().lower()

    if not hmac.compare_digest(email, AUTH_EMAIL):
        return None

    if not hmac.compare_digest(password or "", AUTH_PASSWORD):
        return None

    return AuthUser(email=email, role="admin")


def get_bearer_user(request: Request) -> AuthUser:
    if not AUTH_ENABLED:
        return AuthUser(email="auth-disabled@local", role="admin")

    auth = request.headers.get("authorization") or ""
    prefix = "Bearer "

    if not auth.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = auth[len(prefix):].strip()
    return decode_access_token(token)


def _path_requires_auth(path: str) -> bool:
    """
    Fail-closed route policy.

    Public:
      - "/" so the browser can load the login shell
      - "/auth/login" so the user can submit credentials
      - "/favicon.ico" and "/healthz"
      - "/static/..." so the browser can load frontend JS/CSS assets

    Protected:
      - every "/api/..." operation
      - direct exported artifact access under "/exports/..."
      - "/auth/me" because it reveals the current identity
    """
    if not AUTH_ENABLED:
        return False

    if path in PUBLIC_PATHS:
        return False

    if any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
        return False

    if path == "/auth/login":
        return False

    if path.startswith("/api/"):
        return True

    if path.startswith("/exports/"):
        return True

    if path == "/auth/me":
        return True

    # Unknown app routes are protected by default.
    return True


def install_auth(app: FastAPI) -> None:
    _ensure_auth_config()

    @app.post("/auth/login", response_model=LoginResponse)
    async def login(payload: LoginRequest):
        user = authenticate(payload.email, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail="Incorrect email or password")

        token = create_access_token(user)
        expires_in = AUTH_TOKEN_EXPIRE_MINUTES * 60

        return LoginResponse(
            access_token=token,
            expires_in=expires_in,
            user={"email": user.email, "role": user.role},
        )

    @app.get("/auth/me", response_model=MeResponse)
    async def me(request: Request):
        user = get_bearer_user(request)
        return MeResponse(email=user.email, role=user.role)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        if _path_requires_auth(path):
            try:
                user = get_bearer_user(request)
                request.state.user = user
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response