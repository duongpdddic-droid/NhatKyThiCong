"""TDD: multi-issue tracking per diary (Task 4).

RED phase -- every test here must FAIL against the current codebase until the
multi-issue write path, per-issue photos, /admin/issues dashboard,
per-issue Telegram alerts and multi-issue PDF/Telegram report are implemented.
"""
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
import app.telegram_bot as tbmod
import app.export_pdf as pdfmod

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
    c.execute("DELETE FROM users WHERE username IN ('admin','foreman')")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) "
              "VALUES (?,?,?,?)", ("foreman", "x", "Foreman", "foreman"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) "
              "VALUES (?,?,?,?)", ("admin", "x", "Admin", "admin"))
    c.execute("INSERT INTO recipients (project_id, name, role, telegram_chat_id, "
              "telegram_type) VALUES (1, 'Admin', 'CDT', 'chat-1', 'account')")
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


def _multi_data(date, issues, existing=None):
    """issues: list of (desc, level, workflow).
    existing: optional list of ids to attach to each row (edit mode)."""
    data = {
        "date": date, "weather_temp": "29", "weather_desc": "N",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issue_count": str(len(issues)),
    }
    for i, (desc, level, workflow) in enumerate(issues):
        data[f"issue_{i}_desc"] = desc
        data[f"issue_{i}_level"] = level
        data[f"issue_{i}_workflow"] = workflow
        data[f"issue_{i}_cause"] = ""
        data[f"issue_{i}_related"] = ""
        data[f"issue_{i}_suggestion"] = ""
        data[f"issue_{i}_resolved"] = ""
        data[f"issue_{i}_id"] = str(existing[i]) if existing and i < len(existing) else ""
        data[f"issue_{i}_photo_ids"] = ""
    return data
# ── 1. Multi-issue create ──────────────────────────────────────────────────────

def test_create_diary_writes_multiple_child_issues(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-05-01", [
        ("Lỗi thứ nhất", "low", "pending"),
        ("Lỗi thứ hai", "high", "unresolved"),
    ]), follow_redirects=False)
    c = connect()
    rows = c.execute("SELECT * FROM diary_issues WHERE diary_id IN "
                     "(SELECT id FROM diaries WHERE date='2026-05-01') "
                     "ORDER BY id").fetchall()
    c.close()
    assert len(rows) == 2
    assert rows[0]["description"] == "Lỗi thứ nhất"
    assert rows[0]["level"] == "low"
    assert rows[1]["description"] == "Lỗi thứ hai"
    assert rows[1]["level"] == "high"


def test_create_diary_legacy_single_issue_falls_back(client):
    """No issue_count -> legacy fields synthesise one child issue."""
    client.post("/projects/1/diary/save", data={
        "date": "2026-05-02", "weather_temp": "29", "weather_desc": "N",
        "foreman": "CHT", "total_workers": "0", "notes": "",
        "issues": "legacy issue", "issue_level": "medium", "issue_status": "",
        "cause": "c", "related_party": "r", "workflow_status": "pending",
    }, follow_redirects=False)
    c = connect()
    rows = c.execute("SELECT * FROM diary_issues WHERE diary_id IN "
                     "(SELECT id FROM diaries WHERE date='2026-05-02')").fetchall()
    c.close()
    assert len(rows) == 1
    assert rows[0]["description"] == "legacy issue"
    assert rows[0]["level"] == "medium"
    assert rows[0]["cause"] == "c"


# ── 2. Edit replaces child rows in place (durable marker survives) ─────────────

def test_edit_diary_replaces_child_issues(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-05-03", [
        ("A", "low", "pending"),
        ("B", "high", "unresolved"),
    ]), follow_redirects=False)
    c = connect()
    did = c.execute("SELECT id FROM diaries WHERE date='2026-05-03'").fetchone()["id"]
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM diary_issues WHERE diary_id=? ORDER BY id", (did,)).fetchall()]
    c.close()
    r = client.post(f"/projects/1/diary/{did}/save", data=_multi_data("2026-05-03", [
        ("A-mod", "low", "resolved"),
        ("C", "medium", "pending"),
    ], existing=ids), follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    rows = c.execute("SELECT * FROM diary_issues WHERE diary_id=? ORDER BY id",
                     (did,)).fetchall()
    c.close()
    assert len(rows) == 2
    assert rows[0]["description"] == "A-mod"
    assert rows[0]["workflow_status"] == "resolved"
    assert rows[0]["resolved_at"] is not None
    assert rows[1]["description"] == "C"


def test_edit_preserves_notified_marker(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-05-04", [
        ("alerted", "high", "unresolved"),
    ]), follow_redirects=False)
    c = connect()
    did = c.execute("SELECT id FROM diaries WHERE date='2026-05-04'").fetchone()["id"]
    iid = c.execute("SELECT id FROM diary_issues WHERE diary_id=?", (did,)).fetchone()["id"]
    c.execute("UPDATE diary_issues SET notified_at='2026-01-01 00:00:00' WHERE id=?", (iid,))
    c.commit()
    c.close()
    client.post(f"/projects/1/diary/{did}/save", data=_multi_data("2026-05-04", [
        ("alerted-edited", "high", "unresolved"),
    ], existing=[iid]), follow_redirects=False)
    c = connect()
    row = c.execute("SELECT notified_at FROM diary_issues WHERE id=?", (iid,)).fetchone()
    c.close()
    assert row["notified_at"] == "2026-01-01 00:00:00"


# ── 3. Per-issue photos ────────────────────────────────────────────────────────

def test_per_issue_photo_links_to_child_issue(client):
    """An existing issue-kind photo is linked to a child issue on edit."""
    jpeg = _small_jpeg()
    files = {"photos_gallery": ("p1.jpg", jpeg, "image/jpeg")}
    data = {"date": "2026-05-05", "weather_temp": "29", "weather_desc": "N",
            "foreman": "CHT", "total_workers": "0", "notes": "",
            "issues": "seed", "issue_level": "high", "issue_status": "",
            "cause": "", "related_party": "", "workflow_status": "pending",
            "issue_photo_ids": ["0"], "photo_captions": ["issue cap"]}
    client.post("/projects/1/diary/save", data=data, files=files,
                follow_redirects=False)
    c = connect()
    did = c.execute("SELECT id FROM diaries WHERE date='2026-05-05'").fetchone()["id"]
    ph = c.execute("SELECT * FROM diary_photos WHERE diary_id=?", (did,)).fetchone()
    iid0 = c.execute("SELECT id FROM diary_issues WHERE diary_id=?", (did,)).fetchone()["id"]
    c.close()
    edit = _multi_data("2026-05-05", [
        ("issue with photo", "high", "unresolved"),
    ], existing=[iid0])
    edit["issue_0_photo_ids"] = str(ph["id"])
    r = client.post(f"/projects/1/diary/{did}/save", data=edit,
                    follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    iid = c.execute("SELECT id FROM diary_issues WHERE diary_id=?", (did,)).fetchone()["id"]
    ph2 = c.execute("SELECT issue_id, photo_kind, caption FROM diary_photos "
                    "WHERE id=?", (ph["id"],)).fetchone()
    c.close()
    assert ph2["issue_id"] == iid
    assert ph2["photo_kind"] == "issue"
    assert ph2["caption"] == "issue cap"
# ── 4. Dashboard / /admin/issues scope ─────────────────────────────────────────

def test_dashboard_counts_child_issues(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-06-01", [
        ("u1", "high", "unresolved"),
        ("u2", "low", "pending"),
        ("r1", "low", "resolved"),
    ]), follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 200
    assert "2" in r.text  # two unresolved/pending


def test_admin_issues_lists_each_child_row(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-06-02", [
        ("row one", "high", "unresolved"),
        ("row two", "medium", "pending"),
    ]), follow_redirects=False)
    import app.main as _mainmod
    from app.auth import create_access_token as _tok
    from fastapi.testclient import TestClient as _TC
    admin = _TC(_mainmod.app)
    admin.cookies["access_token"] = _tok({"user_id": 2, "role": "admin"})
    r = admin.get("/admin/issues")
    assert r.status_code == 200
    assert "row one" in r.text
    assert "row two" in r.text


# ── 5. Per-issue Telegram alert ────────────────────────────────────────────────

def test_per_issue_alert_fires_once(client):
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        client.post("/projects/1/diary/save", data=_multi_data("2026-07-01", [
            ("alert me", "high", "unresolved"),
            ("quiet", "low", "pending"),
        ]), follow_redirects=False)
        # second save must not re-alert
        client.post("/projects/1/diary/save", data=_multi_data("2026-07-02", [
            ("another", "high", "unresolved"),
        ]), follow_redirects=False)
    assert len(sent) == 2  # one per new high issue


def test_non_high_to_high_alerts_once(client):
    sent = []

    async def fake_send_message(chat_id, text):
        sent.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        client.post("/projects/1/diary/save", data=_multi_data("2026-07-03", [
            ("escalate", "medium", "pending"),
        ]), follow_redirects=False)
        assert len(sent) == 0
        c = connect()
        did = c.execute("SELECT id FROM diaries WHERE date='2026-07-03'").fetchone()["id"]
        iid = c.execute("SELECT id FROM diary_issues WHERE diary_id=?", (did,)).fetchone()["id"]
        c.close()
        client.post(f"/projects/1/diary/{did}/save", data=_multi_data("2026-07-03", [
            ("escalated", "high", "unresolved"),
        ], existing=[iid]), follow_redirects=False)
    assert len(sent) == 1


# ── 6. Multi-issue PDF + Telegram report ───────────────────────────────────────

def _pdf_text(path):
    import re
    import zlib
    import base64
    raw = open(path, "rb").read()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        blob = m.group(1).strip()
        text = None
        for decode in (zlib.decompress, lambda b: zlib.decompress(base64.a85decode(b, adobe=True))):
            try:
                text = decode(blob).decode("latin-1")
                break
            except Exception:
                continue
        if text is None:
            text = blob.decode("latin-1", "replace")
        out.append(text)
    return "\n".join(out)


def test_pdf_lists_all_issues(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-08-01", [
        ("pdf issue one", "low", "pending"),
        ("pdf issue two", "high", "unresolved"),
    ]), follow_redirects=False)
    c = connect()
    did = c.execute("SELECT id FROM diaries WHERE date='2026-08-01'").fetchone()["id"]
    c.close()
    path = pdfmod.generate_diary_pdf(1, did)
    assert path
    text = _pdf_text(path)
    assert "pdf issue one" in text
    assert "pdf issue two" in text


def test_telegram_report_lists_all_issues(client):
    client.post("/projects/1/diary/save", data=_multi_data("2026-08-02", [
        ("tg issue one", "low", "pending"),
        ("tg issue two", "medium", "unresolved"),
    ]), follow_redirects=False)
    c = connect()
    did = c.execute("SELECT id FROM diaries WHERE date='2026-08-02'").fetchone()["id"]
    c.close()
    captured = []

    async def fake_send_message(chat_id, text):
        captured.append(text)
        return {"ok": True, "error": None}

    with patch.object(tbmod, "send_message", fake_send_message), \
         patch.object(tbmod, "get_bot_token", return_value="123:TOK"):
        import asyncio
        summary = asyncio.run(tbmod.send_diary_report(1, did))
    assert summary.get("ok")
    assert any("tg issue one" in t for t in captured)
    assert any("tg issue two" in t for t in captured)
