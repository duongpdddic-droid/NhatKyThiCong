"""Authentication — JWT + password hashing (bcrypt, legacy SHA-256 upgrade)."""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext

# SECRET_KEY must be provided via environment variable — no hardcoded fallback.
# Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable is required but not set")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# bcrypt only reads the first 72 bytes of a password: bcrypt>=4.1 raises instead
# of truncating, so the cut is applied here to keep hashing and verification
# identical on every backend build.
BCRYPT_MAX_BYTES = 72

security = HTTPBearer(auto_error=False)

# bcrypt password hashing via passlib
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _prepare_password(password: str | bytes | None) -> bytes:
    raw = password if isinstance(password, bytes) else (password or "").encode("utf-8")
    return raw[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return _pwd_context.hash(_prepare_password(password))


def _legacy_sha256_digest(password: str | None) -> str:
    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()


def _is_legacy_sha256(hashed: str | None) -> bool:
    """True for the unsalted 64-hex SHA-256 digests written by the first release."""
    if not hashed or len(hashed) != 64:
        return False
    return all(ch in "0123456789abcdef" for ch in hashed.lower())


def password_needs_rehash(hashed: str | None) -> bool:
    """True when a stored hash predates bcrypt and must be upgraded."""
    return _is_legacy_sha256(hashed)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify against a bcrypt hash or a legacy SHA-256 digest.

    Legacy digests keep existing accounts (the seeded admin included) able to log
    in after the bcrypt migration; ``login`` re-hashes them on success. Unknown
    or corrupt formats must deny — never raise a 500.
    """
    if not hashed:
        return False
    if _is_legacy_sha256(hashed):
        return hmac.compare_digest(_legacy_sha256_digest(plain), hashed.lower())
    try:
        return _pwd_context.verify(_prepare_password(plain), hashed)
    except (ValueError, TypeError):
        return False


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user(request: Request):
    # Check cookie first, then Authorization header
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from app.db import connect
    c = connect()
    try:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_role(*roles):
    def dep(request: Request):
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=302, headers={"Location": "/login"})
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Không có quyền")
        return user
    return dep


# FastAPI resolves the path parameter ``project_id`` / ``diary_id`` and the
# injected ``request`` into these thin callables, so the authorization check
# is bound to the actual URL the caller used rather than a value captured at
# import time.  Routes declare them via ``Annotated[..., Depends(...)]``.
def project_access_dep(project_id: int, request: Request):
    return require_project_access(project_id, request)


def diary_access_dep(diary_id: int, request: Request):
    return require_diary_access(diary_id, request)


# Read-only roles (viewer / supervisor / investor) may READ assigned projects
# and diaries but never create, edit, delete or send.  Only admin and foreman
# hold write rights; the check runs after project/diary access so an
# unassigned writer is still denied, and a read-only member of an assigned
# project is denied the write even though they can see the project.
_WRITER_ROLES = ("admin", "foreman")


def project_write_dep(project_id: int, request: Request):
    user = require_project_access(project_id, request)
    if user["role"] not in _WRITER_ROLES:
        raise HTTPException(status_code=403, detail="Chỉ người dùng có quyền ghi mới thực hiện được thao tác này")
    return user


def diary_write_dep(diary_id: int, request: Request):
    user = require_diary_access(diary_id, request)
    if user["role"] not in _WRITER_ROLES:
        raise HTTPException(status_code=403, detail="Chỉ người dùng có quyền ghi mới thực hiện được thao tác này")
    return user


def _is_admin(user):
    return bool(user) and user.get("role") == "admin"


def require_project_access(project_id: int, request: Request):
    """Server-side project authorization dependency.

    ``project_id`` is injected by FastAPI from the route's path parameter, so
    the check is bound to the actual URL the caller used.  Admins bypass
    assignment; every other role must be present in ``user_projects`` for
    ``project_id``.  Denial is 403 (or 302 to /login when unauthenticated),
    enforced on every protected route so hidden template controls can never
    grant access.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if _is_admin(user):
        return user
    from app.db import connect
    c = connect()
    try:
        row = c.execute(
            "SELECT 1 FROM user_projects WHERE user_id=? AND project_id=?",
            (user["id"], project_id)).fetchone()
    finally:
        c.close()
    if not row:
        raise HTTPException(status_code=403, detail="Không có quyền truy cập dự án này")
    return user


def require_diary_access(diary_id: int, request: Request):
    """Authorization keyed on the diary's owning project.

    Used by edit/save/delete/preview/pdf/send-telegram: the caller must be
    assigned to the project that *owns* the diary (or be admin).  A non-existent
    diary yields 404 so callers cannot probe for project ids.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if _is_admin(user):
        return user
    from app.db import connect
    c = connect()
    try:
        row = c.execute(
            "SELECT project_id FROM diaries WHERE id=?", (diary_id,)).fetchone()
    finally:
        c.close()
    if not row:
        raise HTTPException(status_code=404, detail="Nhật ký không tồn tại")
    project_id = row["project_id"]
    c = connect()
    try:
        row2 = c.execute(
            "SELECT 1 FROM user_projects WHERE user_id=? AND project_id=?",
            (user["id"], project_id)).fetchone()
    finally:
        c.close()
    if not row2:
        raise HTTPException(status_code=403, detail="Không có quyền truy cập dự án này")
    return user
