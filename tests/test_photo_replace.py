"""Tests for existing-photo replace / delete / caption behavior."""
import os
import sys
import hashlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SECRET_KEY = "test-secret"
os.environ["SECRET_KEY"] = SECRET_KEY

import app.db as dbmod  # noqa: E402
import app.main as mainmod  # noqa: E402

DB_PATH = BASE / f"nhatky_test_{int(time.time())}.db"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = BASE / "uploads"

from app.main import app, UPLOAD_DIR  # noqa: E402
from app.auth import create_access_token  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(scope="module")
def client():
    dbmod.init_db()
    c = connect()
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        ("viewer", hashlib.sha256("pass".encode()).hexdigest(), "Viewer", "viewer"))
    c.commit()
    c.close()

    token = create_access_token({"user_id": 1, "role": "viewer"})
    with TestClient(app) as tc:
        tc.cookies["access_token"] = token
        yield tc


@pytest.fixture(autouse=True)
def fresh_photo(client):
    """Seed one existing photo + file per test, cleaned up afterwards."""
    os.makedirs(str(UPLOAD_DIR), exist_ok=True)
    name = "old_photo.png"
    with open(UPLOAD_DIR / name, "wb") as f:
        f.write(b"old-bytes")
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)",
              (1, "2026-01-01"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-01-01'").fetchone()["id"]
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, name, "Old caption"))
    c.commit()
    pid = c.execute("SELECT id FROM diary_photos WHERE filename=?", (name,)).fetchone()["id"]
    c.close()
    yield {"pid": pid, "diary_id": did, "name": name}
    # cleanup (child rows before parent diary)
    c = connect()
    c.execute("DELETE FROM diary_photos WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diary_labor WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diary_machinery WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diary_materials WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diary_tasks WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diaries WHERE id=?", (did,))
    c.commit()
    c.close()
    for f in UPLOAD_DIR.glob("*.png"):
        try:
            os.remove(f)
        except OSError:
            pass


def _row(pid):
    c = connect()
    try:
        r = c.execute("SELECT * FROM diary_photos WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def test_replace_existing_photo_keeps_record_and_removes_old(client, fresh_photo):
    pid = fresh_photo
    files = {"existing_photo_replace": ("new.png", b"new-bytes", "image/png")}
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [str(pid)],
        "existing_photo_captions": ["Replaced caption"],
    }
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302

    row = _row(pid)
    assert row is not None, "record must be kept (same id)"
    assert row["caption"] == "Replaced caption"
    assert row["filename"] != "old_photo.png"
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"new-bytes"
    assert not (UPLOAD_DIR / "old_photo.png").exists(), "old file removed after successful write"


def test_replace_preserves_caption_when_none_given(client, fresh_photo):
    pid = fresh_photo
    files = {"existing_photo_replace": ("new2.png", b"new2", "image/png")}
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [str(pid)],
        "existing_photo_captions": [""],
    }
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row["filename"] != "old_photo.png"
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"new2"


def test_delete_existing_photo_removes_record_and_file(client, fresh_photo):
    pid = fresh_photo
    old_name = _row(pid)["filename"]
    assert (UPLOAD_DIR / old_name).exists()
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [],
        "existing_photo_captions": [],
    }
    r = client.post("/projects/1/diary/save", data=data)
    assert r.status_code == 302
    assert _row(pid) is None
    assert not (UPLOAD_DIR / old_name).exists()


def test_keep_existing_photo_updates_caption_only(client, fresh_photo):
    pid = fresh_photo
    old_name = _row(pid)["filename"]
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [str(pid)],
        "existing_photo_captions": ["Updated caption"],
    }
    r = client.post("/projects/1/diary/save", data=data)
    assert r.status_code == 302
    row = _row(pid)
    assert row["caption"] == "Updated caption"
    assert row["filename"] == old_name


def test_edit_save_route_handles_replace_delete_caption(client, fresh_photo):
    pid = fresh_photo
    old_name = _row(pid)["filename"]
    files = {"existing_photo_replace": ("edit_new.png", b"edit-new", "image/png")}
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [str(pid)],
        "existing_photo_captions": ["Edit caption"],
    }
    # Edit save route is /projects/{pid}/diary/{diary_id}/save
    r = client.post("/projects/1/diary/1/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row["caption"] == "Edit caption"
    assert row["filename"] != old_name
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"edit-new"
    assert not (UPLOAD_DIR / old_name).exists()


def test_edit_save_route_deletes_existing_photo(client, fresh_photo):
    pid = fresh_photo
    old_name = _row(pid)["filename"]
    data = {
        "date": "2026-01-01",
        "existing_photo_ids": [],
        "existing_photo_captions": [],
    }
    r = client.post("/projects/1/diary/1/save", data=data)
    assert r.status_code == 302
    assert _row(pid) is None
    assert not (UPLOAD_DIR / old_name).exists()