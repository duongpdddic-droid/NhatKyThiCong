"""Tests for R2 storage helper with mocked S3 client."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.r2 as r2mod


class _BytesBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def test_disabled_mode_uses_local_fs():
    tmp = Path(tempfile.mkdtemp())
    r2mod.LOCAL_DIR = tmp
    r2mod._client = None
    with patch.dict(os.environ, {}, clear=True):
        r2mod._client = None
        assert r2mod.enabled() is False
        r2mod.save_file("a.png", b"hello")
        assert (tmp / "a.png").read_bytes() == b"hello"
        assert r2mod.read_file("a.png") == b"hello"
        assert r2mod.file_exists("a.png") is True
        r2mod.delete_file("a.png")
        assert not (tmp / "a.png").exists()


def test_enabled_mode_routes_to_boto3():
    pytest.importorskip("boto3")
    tmp = Path(tempfile.mkdtemp())
    r2mod.LOCAL_DIR = tmp
    fake_client = MagicMock()
    fake_client.put_object.return_value = {}
    fake_client.get_object.return_value = {"Body": _BytesBody(b"img-bytes")}
    fake_client.head_object.return_value = {}
    fake_client.delete_object.return_value = {}
    with patch.dict(os.environ, {
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
        "R2_BUCKET": "bkt",
    }):
        r2mod._client = None
        assert r2mod.enabled() is True
        with patch("boto3.client", return_value=fake_client):
            r2mod._client = None
            assert r2mod._client_instance() is fake_client
            r2mod.save_file("x.png", b"data")
            fake_client.put_object.assert_called_once()
            assert r2mod.read_file("x.png") == b"img-bytes"
            assert r2mod.file_exists("x.png") is True
            assert r2mod.delete_file("x.png") is True
            assert r2mod.delete_object("y.png") is True


def test_read_file_falls_back_to_local_when_r2_absent():
    """R2 enabled but object absent → falls back to LOCAL_DIR for legacy files."""
    pytest.importorskip("boto3")
    tmp = Path(tempfile.mkdtemp())
    r2mod.LOCAL_DIR = tmp
    (tmp / "legacy.png").write_bytes(b"local-bytes")
    fake_client = MagicMock()
    fake_client.get_object.return_value = None
    with patch.dict(os.environ, {
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
        "R2_BUCKET": "bkt",
    }):
        r2mod._client = None
        assert r2mod.enabled() is True
        with patch("boto3.client", return_value=fake_client):
            r2mod._client = None
            assert r2mod._client_instance() is fake_client
            assert r2mod.read_file("legacy.png") == b"local-bytes"
            fake_client.get_object.assert_called_once()


def test_delete_diary_objects_skips_empty():
    tmp = Path(tempfile.mkdtemp())
    r2mod.LOCAL_DIR = tmp
    r2mod._client = None
    with patch.dict(os.environ, {}, clear=True):
        r2mod._client = None
        removed = r2mod.delete_diary_objects(1, ["", None])
        assert removed == []


def test_content_type_map():
    assert r2mod.CONTENT_TYPES[".png"] == "image/png"
    assert r2mod.CONTENT_TYPES[".jpg"] == "image/jpeg"
    assert r2mod.CONTENT_TYPES[".webp"] == "image/webp"


# ── /uploads route: auth + local fallback ─────────────────────────────────────

import hashlib
from fastapi.testclient import TestClient

import app.db as dbmod
import app.main as mainmod

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.main import app, UPLOAD_DIR
from app.auth import create_access_token
from app.db import connect


@pytest.fixture(scope="module")
def client():
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    dbmod.DB_PATH = DB_PATH
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute("DELETE FROM users WHERE username='admin'")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(app) as tc:
        tc.cookies["access_token"] = token
        yield tc


def test_uploads_route_requires_auth():
    """Unauthenticated requests to /uploads/{filename} must be redirected."""
    client = TestClient(app)
    r = client.get("/uploads/some.png", follow_redirects=False)
    assert r.status_code in (302, 401, 403)


def test_uploads_route_local_fallback_when_r2_absent(client):
    """When R2 is disabled and the file exists locally, the route serves it."""
    TEST_UPLOAD.mkdir(parents=True, exist_ok=True)
    (TEST_UPLOAD / "local.png").write_bytes(b"local-payload")
    r = client.get("/uploads/local.png", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b"local-payload"
    assert r.headers["content-type"] == "image/png"


def test_uploads_route_returns_404_when_missing(client):
    r = client.get("/uploads/does-not-exist.png", follow_redirects=False)
    assert r.status_code == 404