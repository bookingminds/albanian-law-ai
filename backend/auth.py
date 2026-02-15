"""Authentication: JWT, password hashing, and dependency injection."""

import bcrypt
import jwt
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyCookie

from backend.config import settings
from backend.database import get_user_by_id, get_user_by_email

security = HTTPBearer(auto_error=False)
cookie_scheme = APIKeyCookie(name="token", auto_error=False)


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
    return jwt.encode(
        payload,
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.InvalidTokenError:
        return None


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    cookie_token: str = Depends(cookie_scheme),
):
    """Return user dict if valid token (Bearer or cookie), else None."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif cookie_token:
        token = cookie_token
    if not token:
        return None
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        return None
    user_id = int(payload["sub"])
    user = await get_user_by_id(user_id)
    if not user:
        return None
    return user


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


async def require_admin(
    user: dict = Depends(get_current_user),
):
    """Require authenticated user with is_admin=1."""
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nevitet akses administratori.",
        )
    return user


async def require_subscription(
    user: dict = Depends(get_current_user),
):
    """Require authenticated user with active subscription OR valid free trial.
    Trial = 3 days from signup (trial_ends_at). After trial ends, or on any premium action (e.g. chat),
    access is blocked and the client should show the paywall."""
    from datetime import datetime
    from backend.database import get_active_subscription, mark_trial_used
    sub = await get_active_subscription(user["id"])
    if sub:
        return user
    # Check free trial: trial_ends_at set and not yet expired, and trial not already used
    trial_ends_at = user.get("trial_ends_at")
    trial_used_at = user.get("trial_used_at")
    if trial_used_at:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Provë falas e përfunduar. Aktivizo abonimin për të vazhduar.",
        )
    if trial_ends_at:
        try:
            end = datetime.fromisoformat(trial_ends_at.replace("Z", "").strip())
            now = datetime.utcnow()
            if now < end:
                return user
            # Trial just expired – mark as used
            await mark_trial_used(user["id"], trial_ends_at)
        except Exception:
            pass
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Provë falas e përfunduar. Aktivizo abonimin për të vazhduar.",
    )
