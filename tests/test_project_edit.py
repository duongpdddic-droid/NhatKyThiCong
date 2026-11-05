"""Tests for project edit/update and library soft-disable."""
import os, sys, hashlib, tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

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
    c.execute("INSERT INTO projects (name, address) VALUES (?,?)", ("Old Name", "Old Addr"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("testadmin", hashlib.sha256("pass".encode()).hexdigest(), "Admin", "admin"))
    c.commit()
    c.close()
    token = create_access_token({"user_id": 2, "role": "admin"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


def test_project_edit_page_renders(client):
    r = client.get("/projects/1/edit")
    assert r.status_code == 200
    assert "Old Name" in r.text


def test_project_update(client):
    r = client.post("/projects/1/edit", data={
        "name": "New Name", "address": "New Addr", "investor": "Inv",
        "contractor": "Ctr", "supervisor": "Sup", "start_date": "2026-01-01", "end_date": "2026-12-31",
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    p = dict(c.execute("SELECT * FROM projects WHERE id=1").fetchone())
    c.close()
    assert p["name"] == "New Name"
    assert p["address"] == "New Addr"


def test_library_soft_disable_preserves_type(client):
    # disable machinery type 1
    r = client.post("/admin/library/machinery/1/toggle", follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    t = dict(c.execute("SELECT * FROM machinery_types WHERE id=1").fetchone())
    c.close()
    assert t["is_active"] == 0
    # re-enable
    r = client.post("/admin/library/machinery/1/toggle", follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    t = dict(c.execute("SELECT * FROM machinery_types WHERE id=1").fetchone())
    c.close()
    assert t["is_active"] == 1


def test_library_edit(client):
    r = client.post("/admin/library/machinery/1/edit", data={"name": "Máy xúc (đổi tên)", "unit": "máy"}, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    t = dict(c.execute("SELECT * FROM machinery_types WHERE id=1").fetchone())
    c.close()
    assert t["name"] == "Máy xúc (đổi tên)"


def test_project_edit_requires_admin():
    c = TestClient(mainmod.app)
    r = c.get("/projects/1/edit", follow_redirects=False)
    assert r.status_code in (302, 401, 403)

def test_project_delete_cleans_up_photo_objects(client):
    # Seed a diary with a photo in the project
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?,?,0)", (1, "2026-07-01"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-07-01'").fetchone()["id"]
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)", (did, "pic.png", "cap"))
    c.commit()
    c.close()

    with patch("app.r2.delete_diary_objects") as mock_cleanup:
        r = client.post("/projects/1/delete", follow_redirects=False)
        assert r.status_code == 302
        assert mock_cleanup.called
        # called with the diary id and its photo filename
        args = mock_cleanup.call_args
        assert args[0][0] == did
        assert "pic.png" in args[0][1]
