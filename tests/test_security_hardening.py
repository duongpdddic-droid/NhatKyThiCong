"""Security hardening regressions: bcrypt migration, cookie flags, VN clock.

Covers the open audit findings that were implemented on top of the SHA-256
release: password hashes must move to bcrypt without locking existing accounts
out, the login cookie must only be ``Secure`` on real HTTPS, and the reminder
clock must stay timezone-aware even without a tz database.
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.db as dbmod
import app.main as mainmod
import app.r2 as r2mod
import app.telegram_bot as tb

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_security.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import (
    create_access_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.db import connect

LEGACY_PASSWORD = "admin123"
LONG_PASSWORD = "mật-khẩu-rất-dài-" * 8  # well over bcrypt's 72-byte ceiling


def _legacy_digest(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


@pytest.fixture(scope="module")
def client():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("DELETE FROM users WHERE username IN ('legacy','longpass')")
    # Accounts created by the SHA-256 release.
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("legacy", _legacy_digest(LEGACY_PASSWORD), "Legacy", "admin"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("longpass", hash_password(LONG_PASSWORD), "Long", "foreman"))
    c.commit()
    c.close()
    # Bare TestClient: no app lifespan -> no background reminder thread shared
    # with other test modules.
    tc = TestClient(mainmod.app)
    try:
        yield tc
    finally:
        tc.close()


def _hash_of(username):
    c = connect()
    try:
        row = c.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return row["password_hash"] if row else None
    finally:
        c.close()


def test_hash_password_produces_bcrypt_and_needs_no_rehash():
    hashed = hash_password(LEGACY_PASSWORD)
    assert hashed.startswith("$2")
    assert verify_password(LEGACY_PASSWORD, hashed)
    assert not verify_password("wrong", hashed)
    assert password_needs_rehash(hashed) is False


def test_legacy_sha256_digest_still_verifies():
    digest = _legacy_digest("old-secret")
    assert verify_password("old-secret", digest) is True
    assert verify_password("other", digest) is False
    assert password_needs_rehash(digest) is True


def test_unknown_hash_format_denies_instead_of_raising():
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "") is False
    assert verify_password("anything", None) is False


def test_password_longer_than_bcrypt_limit_is_usable():
    """bcrypt reads at most 72 bytes; hashing/verifying bytes past that must not
    raise (bcrypt>=4.1 does) and must stay deterministic."""
    hashed = hash_password(LONG_PASSWORD)
    assert verify_password(LONG_PASSWORD, hashed) is True
    # Same length, different content inside the 72-byte window -> rejected.
    assert verify_password("X" + LONG_PASSWORD[1:], hashed) is False


def test_login_with_legacy_hash_succeeds_and_upgrades_to_bcrypt(client):
    assert password_needs_rehash(_hash_of("legacy")) is True
    r = client.post("/login", data={"username": "legacy", "password": LEGACY_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "access_token" in r.cookies or "access_token" in r.headers.get("set-cookie", "")
    upgraded = _hash_of("legacy")
    assert upgraded.startswith("$2"), "legacy SHA-256 digest was not migrated on login"
    assert verify_password(LEGACY_PASSWORD, upgraded) is True


def test_login_rejects_wrong_password_without_touching_hash(client):
    before = _hash_of("legacy")
    r = client.post("/login", data={"username": "legacy", "password": "wrong"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "Sai tài khoản" in r.text
    assert _hash_of("legacy") == before


def test_long_password_login_does_not_error(client):
    r = client.post("/login", data={"username": "longpass", "password": LONG_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 302


def test_login_cookie_is_not_secure_over_plain_http(client):
    r = client.post("/login", data={"username": "longpass", "password": LONG_PASSWORD},
                    follow_redirects=False)
    cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")
    assert "Secure" not in cookie


def test_login_cookie_is_secure_over_https():
    tc = TestClient(mainmod.app, base_url="https://testserver")
    try:
        r = tc.post("/login", data={"username": "longpass", "password": LONG_PASSWORD},
                    follow_redirects=False)
        assert r.status_code == 302
        assert "Secure" in r.headers.get("set-cookie", "")
    finally:
        tc.close()


def test_access_token_has_valid_future_expiry():
    token = create_access_token({"user_id": 1, "role": "admin"})
    payload = decode_token(token)
    assert payload and payload["user_id"] == 1
    import datetime as dt
    assert payload["exp"] > int(dt.datetime.now(dt.timezone.utc).timestamp())


def test_vn_now_is_timezone_aware():
    now = tb._vn_now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 7 * 3600


def test_vn_now_fallback_without_zoneinfo_is_still_aware():
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "zoneinfo":
            raise ImportError("no tz database on this platform")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        now = tb._vn_now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 7 * 3600


class _FakeConn:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.closed = 0

    def execute(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed += 1


def test_get_bot_token_returns_pooled_connection_when_query_fails(monkeypatch):
    """A failing settings query must not burn a pool slot (regression)."""
    conn = _FakeConn(error=RuntimeError("db down"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(dbmod, "connect", lambda: conn)

    assert tb.get_bot_token() == ""
    assert conn.closed == 1, "connection leaked: close() was not called on the error path"


def test_get_bot_token_closes_connection_on_success(monkeypatch):
    conn = _FakeConn(row={"value": "123:ABCdef_ghi"})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(dbmod, "connect", lambda: conn)

    assert tb.get_bot_token() == "123:ABCdef_ghi"
    assert conn.closed == 1
