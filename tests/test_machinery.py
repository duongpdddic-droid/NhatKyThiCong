"""Tests for machinery quantity migration (hours column aliased to quantity)."""
import os, sys, hashlib, tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.db as dbmod
import app.main as mainmod

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
import app.r2 as r2mod
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.main import app, UPLOAD_DIR
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


def _machinery_rows(diary_id):
    c = connect()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM diary_machinery WHERE diary_id=?", (diary_id,))]
    finally:
        c.close()


def test_seed_unit_is_machinery(client):
    c = connect()
    try:
        rows = [dict(r) for r in c.execute("SELECT unit FROM machinery_types")]
    finally:
        c.close()
    assert all(r["unit"] == "máy" for r in rows)
    assert len(rows) >= 8


def test_migrate_legacy_hour_to_may(client):
    """Insert a legacy 'giờ' row, re-run init, assert it becomes 'máy'."""
    c = connect()
    try:
        c.execute("INSERT OR IGNORE INTO machinery_types (name, unit, sort_order) VALUES (?,?,?)",
                  ("Legacy hour machine", "giờ", 99))
        c.commit()
    finally:
        c.close()
    dbmod.init_db()
    c = connect()
    try:
        row = c.execute("SELECT unit FROM machinery_types WHERE name='Legacy hour machine'").fetchone()
    finally:
        c.close()
    assert row["unit"] == "máy"


def test_save_machinery_as_integer(client):
    c = connect()
    try:
        c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, "2026-03-01"))
        did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-03-01'").fetchone()["id"]
        c.commit()
    finally:
        c.close()
    data = {"date": "2026-03-01", "machinery_ids": "1", "machinery_1": "3"}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    rows = _machinery_rows(did)
    assert len(rows) == 1
    assert rows[0]["hours"] == 3
    assert float(rows[0]["hours"]).is_integer()


def test_save_machinery_float_rejected(client):
    c = connect()
    try:
        c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, "2026-03-02"))
        did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-03-02'").fetchone()["id"]
        c.commit()
    finally:
        c.close()
    data = {"date": "2026-03-02", "machinery_ids": "1", "machinery_1": "2.5"}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    rows = _machinery_rows(did)
    assert rows == []


def test_edit_save_machinery_integer(client):
    c = connect()
    try:
        c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, "2026-03-03"))
        did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-03-03'").fetchone()["id"]
        c.commit()
    finally:
        c.close()
    data = {"date": "2026-03-03", "machinery_ids": "1", "machinery_1": "5"}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    rows = _machinery_rows(did)
    assert len(rows) == 1
    assert rows[0]["hours"] == 5


def test_preview_machinery_label(client):
    c = connect()
    try:
        c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, "2026-03-04"))
        did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-03-04'").fetchone()["id"]
        c.commit()
    finally:
        c.close()
    data = {"date": "2026-03-04", "machinery_ids": "1", "machinery_1": "4"}
    client.post(f"/projects/1/diary/{did}/save", data=data, follow_redirects=False)
    r = client.get(f"/projects/1/diary/{did}/preview")
    assert r.status_code == 200
    assert "Số lượng" in r.text
    assert "Số giờ" not in r.text