"""Regression: diary edit form must preserve fields and tasks."""
import os, sys, hashlib, tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.db as dbmod
import app.main as mainmod
import app.r2 as r2mod

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import create_access_token
from app.db import connect


@pytest.fixture(scope="module")
def client():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("viewer", hashlib.sha256("pass".encode()).hexdigest(), "Viewer", "viewer"))
    c.commit()
    # seed a diary with notes/issues/tasks
    c.execute("INSERT INTO diaries (project_id, date, weather_temp, weather_desc, foreman, total_workers, notes, issues, issue_level, issue_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (1, "2026-05-01", 30.5, "Nắng", "CHT A", 12, "Ghi chu test", "Vuong mac", "medium", "Dang xu ly"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-05-01'").fetchone()["id"]
    c.execute("INSERT INTO diary_tasks (diary_id, task_name, quantity, unit, cumulative_pct) VALUES (?,?,?,?,?)",
              (did, "Be tong", 10.5, "m3", 50.0))
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "viewer"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc, did


def test_edit_form_prefills_fields(client):
    tc, did = client
    r = tc.get(f"/projects/1/diary/{did}/edit")
    assert r.status_code == 200
    assert "Ghi chu test" in r.text
    assert "Vuong mac" in r.text
    assert "Dang xu ly" in r.text
    assert "CHT A" in r.text
    assert "30.5" in r.text
    assert "Nắng" in r.text


def test_edit_form_renders_tasks(client):
    tc, did = client
    r = tc.get(f"/projects/1/diary/{did}/edit")
    assert r.status_code == 200
    assert "Be tong" in r.text
    assert 'name="task_name"' in r.text


def test_edit_save_preserves_tasks(client):
    tc, did = client
    r = tc.post(f"/projects/1/diary/{did}/save", data={
        "date": "2026-05-01",
        "weather_temp": "31.0",
        "weather_desc": "Mua",
        "foreman": "CHT B",
        "total_workers": "0",
        "notes": "Ghi chu moi",
        "issues": "Van de moi",
        "issue_level": "high",
        "issue_status": "Da xu ly",
        "task_name": ["Be tong", "Xay tuong"],
        "task_qty": ["10.5", "5"],
        "task_unit": ["m3", "m2"],
        "task_pct": ["50", "30"],
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    tasks = [dict(x) for x in c.execute("SELECT * FROM diary_tasks WHERE diary_id=?", (did,)).fetchall()]
    c.close()
    assert len(tasks) == 2
    names = {t["task_name"] for t in tasks}
    assert "Be tong" in names and "Xay tuong" in names


def test_diary_form_buttons_reference_app_js_helpers():
    """G2: the form's addTask/addMaterial handlers resolve to real functions."""
    src = Path(BASE / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function addTask()" in src
    assert "function addMaterial()" in src