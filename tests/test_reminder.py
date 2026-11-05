"""Tests for the diary reminder feature (logic, routes, timezone)."""
import os, sys, hashlib, tempfile, asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

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
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def client():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = dbmod.connect()
    c.execute("INSERT INTO projects (name, address) VALUES (?,?)", ("Test Project", "Addr"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("viewer", hashlib.sha256("pass".encode()).hexdigest(), "Viewer", "viewer"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("admin2", hashlib.sha256("pass".encode()).hexdigest(), "Admin2", "admin"))
    c.commit()
    c.close()
    from app.auth import create_access_token
    token = create_access_token({"user_id": 3, "role": "admin"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


def test_vn_now_returns_timezone_aware():
    now = tb._vn_now()
    assert now.tzinfo is not None
    assert "Asia/Ho_Chi_Minh" in str(now.tzinfo) or now.utcoffset().total_seconds() == 7 * 3600


def test_send_reminder_no_recipients():
    def fake_execute(sql, params=None):
        s = (sql or "").upper()
        m = MagicMock()
        m.__iter__.return_value = iter([])
        m.fetchall.return_value = []
        m.fetchone.return_value = None
        # project row
        if "FROM PROJECTS" in s:
            m.fetchone.return_value = {"id": 1, "name": "P", "address": "", "reminder_enabled": 1, "reminder_time": "00:00"}
        return m
    with patch.object(tb, "connect") as mock_connect, \
         patch.object(tb, "_vn_now", return_value=__import__("datetime").datetime(2026, 1, 2, 12, 0)):
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn
        result = _run(tb.send_reminder(1, force=True))
    assert result["ok"] is False
    assert "người nhận" in result["error"] or "Telegram" in result["error"]


def test_send_reminder_already_has_diary():
    from datetime import datetime
    diary_row = {"id": 99}
    project_row = {"id": 1, "name": "P", "address": "", "reminder_enabled": 1, "reminder_time": "00:00"}

    def fake_execute(sql, params=None):
        s = (sql or "").upper()
        m = MagicMock()
        m.__iter__.return_value = iter([])
        m.fetchall.return_value = []
        if "FROM PROJECTS" in s:
            m.fetchone.return_value = project_row
        elif "FROM DIARIES" in s and "PROJECT_ID=?" in s and "DATE=?" in s:
            m.fetchone.return_value = diary_row
        elif "REMINDER_LOG" in s:
            m.fetchone.return_value = None
        return m

    with patch.object(tb, "connect") as mock_connect, \
         patch.object(tb, "_vn_now", return_value=datetime(2026, 1, 2, 12, 0)):
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        conn.commit = MagicMock()
        mock_connect.return_value = conn
        result = _run(tb.send_reminder(1, force=True))
    assert result["ok"] is False
    assert "nhật ký hôm nay" in result["error"]


def test_api_reminders_run_requires_admin():
    c = TestClient(mainmod.app)  # no cookie
    r = c.get("/api/reminders/run", follow_redirects=False)
    assert r.status_code in (302, 401, 403)


def test_api_reminders_status_requires_admin():
    c = TestClient(mainmod.app)
    r = c.get("/api/reminders/status", follow_redirects=False)
    assert r.status_code in (302, 401, 403)


def test_project_reminders_save_route(client):
    r = client.post("/projects/1/reminders", data={"reminder_enabled": "1", "reminder_time": "09:30"}, follow_redirects=False)
    assert r.status_code == 302
    c = dbmod.connect()
    row = c.execute("SELECT enabled, time FROM project_reminders WHERE project_id=1").fetchone()
    c.close()
    assert row is not None
    assert row["enabled"] == 1
    assert row["time"] == "09:30"


def test_admin_reminders_page_renders(client):
    r = client.get("/admin/reminders")
    assert r.status_code == 200
    assert "Nhắc nhở" in r.text


def test_admin_reminders_save_route(client):
    r = client.post("/admin/reminders/1", data={"reminder_enabled": "0", "reminder_time": "07:00"}, follow_redirects=False)
    assert r.status_code == 302
    c = dbmod.connect()
    row = c.execute("SELECT enabled, time FROM project_reminders WHERE project_id=1").fetchone()
    c.close()
    assert row["enabled"] == 0
    assert row["time"] == "07:00"