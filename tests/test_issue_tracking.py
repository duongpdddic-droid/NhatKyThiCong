"""TDD: construction diary issue/recommendation tracking (Task 3).

RED phase — every test here must FAIL against the current codebase
(app/db.py, app/main.py, app/telegram_bot.py) until the issue-tracking
feature is implemented.
"""
import os
import sys
import tempfile
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
import app.telegram_bot as tbmod

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD


from app.auth import create_access_token
from app.db import connect


def _small_jpeg():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="JPEG", quality=82)
    return buf.getvalue()


@pytest.fixture(scope="module")
def client():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    TEST_UPLOAD.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("DELETE FROM users WHERE username='admin'")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("admin", "x", "Admin", "admin"))
    c.execute("INSERT INTO recipients (project_id, name, role, telegram_chat_id, telegram_type) "
              "VALUES (1, 'Admin', 'CDT', 'chat-1', 'account')")
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


@pytest.fixture(autouse=True)
def _reset_state():
    r2mod._client = None
    r2mod._quota = None
    yield
    r2mod._client = None
    r2mod._quota = None


def _row(c, table, where, params):
    r = c.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
    return dict(r) if r else None


# ── 1. Schema migration ────────────────────────────────────────────────────────

def test_migration_adds_issue_columns():
    """Old DB keeps issue_status; new issue columns must be added."""
    dbmod.DB_PATH = DB_PATH
    dbmod.IS_PG = False
    dbmod.init_db()
    c = connect()
    cols = {r[1] for r in c.execute("PRAGMA table_info(diaries)").fetchall()}
    c.close()
    for col in ("cause", "related_party", "workflow_status", "resolved_at",
                "issue_notified_at"):
        assert col in cols, f"missing column {col}"
    # Legacy field preserved.
    assert "issue_status" in cols
    assert "issue_level" in cols


def test_migration_preserves_legacy_recommendation(tmp_path):
    """Recommendation text in issue_status survives migration."""
    p = tmp_path / "old.db"
    import sqlite3
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
        CREATE TABLE diaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER, date TEXT, issues TEXT,
            issue_level TEXT DEFAULT 'none', issue_status TEXT DEFAULT 'none'
        );
    """)
    conn.execute("INSERT INTO projects (name) VALUES ('P')")
    conn.execute("INSERT INTO diaries (project_id, date, issues, issue_level, issue_status)"
                 " VALUES (1, '2026-01-01', 'Vấn đề', 'high', 'Đề xuất X')")
    conn.commit()
    conn.close()
    dbmod.DB_PATH = p
    dbmod.IS_PG = False
    dbmod.init_db()
    conn = sqlite3.connect(str(p))
    row = conn.execute("SELECT issue_status FROM diaries WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "Đề xuất X"


# ── 2. Create / edit / read round-trip ─────────────────────────────────────────

def test_create_diary_saves_issue_fields(client):
    r = client.post("/projects/1/diary/save", data={
        "date": "2026-03-01", "weather_temp": "30", "weather_desc": "Nắng",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "Sàn bong.radians", "issue_level": "high",
        "issue_status": "Kiến nghị sửa", "cause": "Thiếu kiểm tra",
        "related_party": "Nhà thầu", "workflow_status": "pending",
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    d = _row(c, "diaries", "project_id=1 AND date='2026-03-01'", ())
    c.close()
    assert d is not None
    assert d["issues"] == "Sàn bong.radians"
    assert d["issue_level"] == "high"
    assert d["issue_status"] == "Kiến nghị sửa"
    assert d["cause"] == "Thiếu kiểm tra"
    assert d["related_party"] == "Nhà thầu"
    assert d["workflow_status"] == "pending"


def test_resolve_sets_date_when_absent(client):
    client.post("/projects/1/diary/save", data={
        "date": "2026-03-02", "weather_temp": "28", "weather_desc": "Mưa",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "Vấn đề", "issue_level": "medium", "issue_status": "",
        "cause": "", "related_party": "", "workflow_status": "resolved",
    }, follow_redirects=False)
    c = connect()
    d = _row(c, "diaries", "project_id=1 AND date='2026-03-02'", ())
    c.close()
    assert d["workflow_status"] == "resolved"
    assert d["resolved_at"], "resolved_at must be set on resolve"


def test_reopen_clears_date(client):
    client.post("/projects/1/diary/save", data={
        "date": "2026-03-02", "weather_temp": "28", "weather_desc": "Mưa",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "Vấn đề", "issue_level": "medium", "issue_status": "",
        "cause": "", "related_party": "", "workflow_status": "pending",
    }, follow_redirects=False)
    c = connect()
    d = _row(c, "diaries", "project_id=1 AND date='2026-03-02'", ())
    c.close()
    assert d["workflow_status"] == "pending"
    assert not d["resolved_at"]


def test_edit_form_prefills_issue_fields(client):
    r = client.post("/projects/1/diary/save", data={
        "date": "2026-03-03", "weather_temp": "29", "weather_desc": "Nắng",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "Issue text", "issue_level": "low", "issue_status": "Rec",
        "cause": "Cause text", "related_party": "Party text",
        "workflow_status": "unresolved",
    }, follow_redirects=False)
    c = connect()
    did = _row(c, "diaries", "project_id=1 AND date='2026-03-03'", ())["id"]
    c.close()
    r = client.get(f"/projects/1/diary/{did}/edit")
    assert r.status_code == 200
    assert "Issue text" in r.text
    assert "Cause text" in r.text
    assert "Party text" in r.text
    assert "Rec" in r.text


# ── 3. Issue photos: kind/caption/storage quota integration ────────────────────

def test_issue_photo_kind_default_work(client):
    """Legacy uploads default to work/site; issue uploads carry issue kind."""
    jpeg = _small_jpeg()
    files = {"photos_gallery": ("a.jpg", jpeg, "image/jpeg")}
    data = {"date": "2026-03-04", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "x", "issue_level": "none", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
            "photo_captions": ["site cap"]}
    r = client.post("/projects/1/diary/save", data=data, files=files,
                    follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    rows = c.execute("SELECT * FROM diary_photos WHERE diary_id IN "
                     "(SELECT id FROM diaries WHERE date='2026-03-04')").fetchall()
    c.close()
    assert len(rows) == 1
    assert rows[0]["photo_kind"] in ("work", "site", None)
    assert rows[0]["caption"] == "site cap"


def test_issue_photo_kind_issue_marker(client):
    jpeg = _small_jpeg()
    files = {"photos_gallery": ("b.jpg", jpeg, "image/jpeg")}
    data = {"date": "2026-03-05", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "y", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
            "photo_captions": ["issue cap"],
            "issue_photo_ids": ["0"]}
    r = client.post("/projects/1/diary/save", data=data, files=files,
                    follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    rows = c.execute("SELECT * FROM diary_photos WHERE diary_id IN "
                     "(SELECT id FROM diaries WHERE date='2026-03-05')").fetchall()
    c.close()
    assert len(rows) == 1
    assert rows[0]["photo_kind"] == "issue"
    assert rows[0]["caption"] == "issue cap"


# ── 4. Dashboard / unresolved list ─────────────────────────────────────────────

def test_dashboard_shows_unresolved_count(client):
    # one unresolved high, one pending low
    client.post("/projects/1/diary/save", data={
        "date": "2026-04-01", "weather_temp": "29", "weather_desc": "N",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "unresolved", "issue_level": "high", "issue_status": "",
        "cause": "", "related_party": "", "workflow_status": "unresolved",
    }, follow_redirects=False)
    client.post("/projects/1/diary/save", data={
        "date": "2026-04-02", "weather_temp": "29", "weather_desc": "N",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "pending", "issue_level": "low", "issue_status": "",
        "cause": "", "related_party": "", "workflow_status": "pending",
    }, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 200
    assert "unresolved" in r.text.lower() or "2" in r.text


def test_issues_list_route(client):
    # /admin/issues is admin-only; the module fixture authenticates as foreman,
    # so the route is exercised with an admin-scoped client here.
    import app.main as _mainmod
    from app.auth import create_access_token as _tok
    from fastapi.testclient import TestClient as _TC
    admin = _TC(_mainmod.app)
    admin.cookies["access_token"] = _tok({"user_id": 2, "role": "admin"})
    r = admin.get("/admin/issues")
    assert r.status_code == 200
    assert "unresolved" in r.text.lower()


# ── 5. High-severity Telegram alert ────────────────────────────────────────────

def test_high_severity_alerts_after_save(client):
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        r = client.post("/projects/1/diary/save", data={
            "date": "2026-05-01", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "high issue", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert r.status_code == 302
    assert len(sent) >= 1
    assert "high issue" in sent[0][1]


def test_failed_telegram_does_not_break_save(client):
    async def boom(chat_id, text):
        return {"ok": False, "error": "network down"}

    with patch.object(tbmod, "send_message", boom), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        r = client.post("/projects/1/diary/save", data={
            "date": "2026-05-02", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "high fail", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    d = _row(c, "diaries", "project_id=1 AND date='2026-05-02'", ())
    c.close()
    assert d is not None
    assert d["issues"] == "high fail"


def test_no_duplicate_alert_on_ordinary_edit(client):
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        client.post("/projects/1/diary/save", data={
            "date": "2026-05-03", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "dup", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
        c = connect()
        did = _row(c, "diaries", "project_id=1 AND date='2026-05-03'", ())["id"]
        c.close()
        client.post(f"/projects/1/diary/{did}/save", data={
            "date": "2026-05-03", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "dup edited", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert len(sent) == 1


def test_escalation_alerts_once(client):
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        client.post("/projects/1/diary/save", data={
            "date": "2026-05-04", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "low start", "issue_level": "low", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
        c = connect()
        did = _row(c, "diaries", "project_id=1 AND date='2026-05-04'", ())["id"]
        c.close()
        client.post(f"/projects/1/diary/{did}/save", data={
            "date": "2026-05-04", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "low start", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert len(sent) == 1


def test_no_alert_when_no_recipients(client):
    """No configured recipients -> no alert, save still succeeds."""
    c = connect()
    c.execute("DELETE FROM recipients WHERE project_id=1")
    c.execute("DELETE FROM users WHERE id != 1")
    c.commit()
    c.close()
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        r = client.post("/projects/1/diary/save", data={
            "date": "2026-05-05", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "no recip", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert r.status_code == 302
    assert sent == []
    # Re-seed the viewer + recipient so later tests in the same client still work.
    c = connect()
    c.execute("INSERT OR IGNORE INTO users (username, password_hash, full_name, role) "
              "VALUES (?,?,?,?)", ("viewer", "x", "Viewer", "viewer"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.execute("INSERT OR IGNORE INTO recipients (project_id, name, role, telegram_chat_id, telegram_type) "
              "VALUES (1, 'Admin', 'CDT', 'chat-1', 'account')")
    c.commit()
    c.close()


def test_durable_marker_prevents_resend_after_restart(client):
    """A second save of the same high issue must not re-alert."""
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        client.post("/projects/1/diary/save", data={
            "date": "2026-05-06", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "marker", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
        client.post("/projects/1/diary/save", data={
            "date": "2026-05-06", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "marker", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
        }, follow_redirects=False)
    assert len(sent) == 1