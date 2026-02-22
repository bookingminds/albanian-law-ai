"""Authentication: Supabase Auth (primary) + local JWT (fallback).

When SUPABASE_URL is configured, register/login go through Supabase Auth.
Token verification checks Supabase JWT first, then falls back to local JWT.
"""

import bcrypt
import jwt
import httpx
import logging
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyCookie

from backend.config import settings
from backend.database import get_user_by_id, get_user_by_email, get_user_by_supabase_uid

logger = logging.getLogger("rag.auth")

security = HTTPBearer(auto_error=False)
cookie_scheme = APIKeyCookie(name="token", auto_error=False)

_supabase_configured = bool(settings.SUPABASE_URL and settings.SUPABASE_ANON_KEY)

# ── Supabase helpers ──────────────────────────────────────────

def _sb_url(path: str) -> str:
    return f"{settings.SUPABASE_URL.rstrip('/')}{path}"


def _sb_headers(use_service_role: bool = False) -> dict:
    key = settings.SUPABASE_SERVICE_ROLE_KEY if use_service_role else settings.SUPABASE_ANON_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def supabase_sign_up(email: str, password: str) -> dict:
    """Register user via Supabase Auth Admin API (auto-confirms email)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            _sb_url("/auth/v1/admin/users"),
            headers=_sb_headers(use_service_role=True),
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
            },
        )
        data = r.json()
        if r.status_code >= 400:
            msg = data.get("msg") or data.get("error_description") or data.get("message") or str(data)
            if "already been registered" in msg.lower() or "already exists" in msg.lower():
                raise HTTPException(status_code=409, detail="Ky email është tashmë i regjistruar.")
            raise HTTPException(status_code=r.status_code, detail=msg)
        sb_user = data
        session_data = {"user": sb_user, "session": None}
        try:
            login_r = await client.post(
                _sb_url("/auth/v1/token?grant_type=password"),
                headers=_sb_headers(),
                json={"email": email, "password": password},
            )
            if login_r.status_code == 200:
                login_data = login_r.json()
                session_data["session"] = {"access_token": login_data.get("access_token", "")}
        except Exception:
            logger.warning("Auto-login after signup failed, user will need to log in manually")
    return session_data


async def supabase_sign_in(email: str, password: str) -> dict:
    """Login via Supabase Auth. Returns {access_token, user, ...} or raises."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            _sb_url("/auth/v1/token?grant_type=password"),
            headers=_sb_headers(),
            json={"email": email, "password": password},
        )
    data = r.json()
    if r.status_code >= 400:
        msg = data.get("msg") or data.get("error_description") or data.get("message") or "Email ose fjalëkalim i gabuar."
        raise HTTPException(status_code=401, detail=msg)
    return data


async def supabase_reset_password(email: str) -> dict:
    """Send a password reset email via Supabase Auth."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            _sb_url("/auth/v1/recover"),
            headers=_sb_headers(),
            json={"email": email},
        )
    if r.status_code >= 400:
        data = r.json()
        msg = data.get("msg") or data.get("message") or "Gabim gjatë dërgimit."
        raise HTTPException(status_code=r.status_code, detail=msg)
    return {"message": "Email i dërguar me sukses."}


async def supabase_sign_out(access_token: str) -> bool:
    """Revoke a Supabase session (server-side logout)."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            _sb_url("/auth/v1/logout"),
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
    return r.status_code < 400


async def supabase_get_user(access_token: str) -> dict | None:
    """Verify Supabase access token and return user info."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            _sb_url("/auth/v1/user"),
            headers={
                "apikey": settings.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
        )
    if r.status_code == 200:
        return r.json()
    return None


# ── Local JWT helpers (fallback) ──────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int, email: str, is_admin: bool) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "is_admin": is_admin,
        "exp": datetime.utcnow() + timedelta(days=settings.JWT_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None


# ── Dependency: get current user ──────────────────────────────

async def _resolve_user_from_token(token: str) -> dict | None:
    """Try Supabase first, then local JWT."""
    if _supabase_configured:
        sb_user = await supabase_get_user(token)
        if sb_user:
            uid = sb_user.get("id")
            email = sb_user.get("email", "")
            user = await get_user_by_supabase_uid(uid)
            if user:
                return user
            user = await get_user_by_email(email)
            if user:
                from backend.database import link_supabase_uid
                await link_supabase_uid(user["id"], uid)
                user["supabase_uid"] = uid
                return user
            from backend.database import create_user_from_supabase
            from backend.config import settings as _s
            from datetime import timedelta as _td
            trial_end = (datetime.utcnow() + _td(days=_s.TRIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
            new_id = await create_user_from_supabase(
                email, supabase_uid=uid, is_admin=False,
                trial_ends_at=trial_end,
            )
            return await get_user_by_id(new_id)

    payload = decode_token(token)
    if payload and "sub" in payload:
        user_id = int(payload["sub"])
        return await get_user_by_id(user_id)
    return None


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    cookie_token: str = Depends(cookie_scheme),
):
    """Return user dict if valid token, else None."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif cookie_token:
        token = cookie_token
    if not token:
        return None
    return await _resolve_user_from_token(token)


async def get_current_user(
    user: dict | None = Depends(get_current_user_optional),
):
    """Require authenticated user. Raise 401 if not logged in."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nuk jeni i autentifikuar.",
        )
    return user


async def require_admin(user: dict = Depends(get_current_user)):
    """Require authenticated user with is_admin=1."""
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nevitet akses administratori.",
        )
    return user


async def require_subscription(user: dict = Depends(get_current_user)):
    """Require active Google Play subscription OR valid free trial."""
    from backend.database import get_active_subscription, mark_trial_used, set_trial_ends_at
    if user.get("is_admin"):
        return user
    sub = await get_active_subscription(user["id"])
    if sub:
        return user
    trial_used_at = user.get("trial_used_at")
    if trial_used_at:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Provë falas e përfunduar. Aktivizo abonimin për të vazhduar.",
        )
    trial_ends_at = user.get("trial_ends_at")
    if not trial_ends_at or (isinstance(trial_ends_at, str) and not trial_ends_at.strip()):
        new_end = (datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        await set_trial_ends_at(user["id"], new_end)
        return user
    try:
        if isinstance(trial_ends_at, datetime):
            end = trial_ends_at.replace(tzinfo=None)
        else:
            end = datetime.fromisoformat(str(trial_ends_at).replace("Z", "").strip())
        now = datetime.utcnow()
        if now < end:
            return user
        await mark_trial_used(user["id"], str(trial_ends_at))
    except Exception:
        new_end = (datetime.utcnow() + timedelta(days=settings.TRIAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        await set_trial_ends_at(user["id"], new_end)
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Provë falas e përfunduar. Aktivizo abonimin për të vazhduar.",
    )
