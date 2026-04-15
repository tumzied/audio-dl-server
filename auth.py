"""
Authentication module
======================
Supports two schemes — either is sufficient to access protected endpoints:

  1. API Key   →  X-API-Key: <key>       (set via API_KEYS env var)
  2. JWT Bearer →  Authorization: Bearer <token>
                   Obtain a token via POST /auth/token (username + password)

Environment variables
---------------------
  SECRET_KEY                   JWT signing secret (required in production)
  API_KEYS                     Comma-separated static API keys
  ADMIN_USERNAME               Username for the built-in admin account (default: admin)
  ADMIN_PASSWORD               Password for the built-in admin account
  ACCESS_TOKEN_EXPIRE_MINUTES  JWT lifetime in minutes (default: 60)
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

_raw_keys = os.getenv("API_KEYS", "")
VALID_API_KEYS: set[str] = {k.strip() for k in _raw_keys.split(",") if k.strip()}

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-memory user store; keyed by username, value is bcrypt hash.
# Extend to a database as needed.
USERS: dict[str, str] = {}

_admin_user = os.getenv("ADMIN_USERNAME", "admin")
_admin_pass = os.getenv("ADMIN_PASSWORD", "")
if _admin_pass:
    USERS[_admin_user] = pwd_context.hash(_admin_pass)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return jwt.encode({"sub": subject, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def _decode_jwt(token: str) -> str:
    """Return the username (`sub`) from a valid token, or raise 401."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if not username:
            raise ValueError("missing sub claim")
        return username
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# FastAPI security schemes
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


async def require_auth(
    api_key: Optional[str] = Security(api_key_header),
    token: Optional[str] = Security(oauth2_scheme),
) -> dict:
    """
    FastAPI dependency — gate any endpoint behind authentication.
    Accepts a valid API key *or* a valid JWT Bearer token.
    """
    if api_key and api_key in VALID_API_KEYS:
        return {"type": "api_key"}

    if token:
        username = _decode_jwt(token)
        return {"type": "jwt", "username": username}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide 'X-API-Key' header or 'Authorization: Bearer <token>'",
        headers={"WWW-Authenticate": "Bearer"},
    )
