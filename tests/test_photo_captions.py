"""Regression: new-photo captions must stay attached to the right image."""
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
    c.execute("DELETE FROM users WHERE username='admin'")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


def _new_rows(did):
    c = connect()
    try:
        return [dict(x) for x in c.execute(
            "SELECT filename, caption FROM diary_photos WHERE diary_id=? AND filename!='old.png'",
            (did,)).fetchall()]
    finally:
        c.close()


def test_caption_stays_with_image_when_first_row_empty(client):
    # create a fresh diary
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?,?,0)", (1, "2026-02-01"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-02-01'").fetchone()["id"]
    c.commit()
    c.close()

    # Two rows: row0 has no selected image; row1 selects gallery.
    files = [
        ("photos_gallery", ("", b"", "application/octet-stream")),  # empty placeholder
        ("photos_camera", ("", b"", "application/octet-stream")),    # empty placeholder
        ("photos_gallery", ("img1.png", b"img1", "image/png")),
        ("photos_camera", ("", b"", "application/octet-stream")),
    ]
    data = {
        "date": "2026-02-01",
        "photo_captions": ["caption-row0", "caption-row1"],
    }
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rows = _new_rows(did)
    assert len(rows) == 1
    assert rows[0]["caption"] == "caption-row1"

    # cleanup
    c = connect()
    c.execute("DELETE FROM diary_photos WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diaries WHERE id=?", (did,))
    c.commit()
    c.close()


def test_camera_vs_gallery_selection_keeps_caption(client):
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?,?,0)", (1, "2026-02-02"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-02-02'").fetchone()["id"]
    c.commit()
    c.close()

    # Row0: gallery empty, camera selected -> caption0
    # Row1: gallery selected, camera empty -> caption1
    files = [
        ("photos_gallery", ("", b"", "application/octet-stream")),
        ("photos_camera", ("cam.png", b"cam", "image/png")),
        ("photos_gallery", ("gal.png", b"gal", "image/png")),
        ("photos_camera", ("", b"", "application/octet-stream")),
    ]
    data = {
        "date": "2026-02-02",
        "photo_captions": ["cap-cam", "cap-gal"],
    }
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rows = _new_rows(did)
    caps = sorted(r["caption"] for r in rows)
    assert caps == ["cap-cam", "cap-gal"]
    # verify bytes pairing
    for row in rows:
        p = TEST_UPLOAD / row["filename"] if False else None
    # check mapping by reading files
    import app.r2 as r2mod
    for row in rows:
        data_bytes = r2mod.read_file(row["filename"]) or (TEST_UPLOAD / row["filename"]).read_bytes()
        if data_bytes == b"cam":
            assert row["caption"] == "cap-cam"
        if data_bytes == b"gal":
            assert row["caption"] == "cap-gal"

    c = connect()
    c.execute("DELETE FROM diary_photos WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diaries WHERE id=?", (did,))
    c.commit()
    c.close()


def test_legacy_photos_chunking(client):
    # legacy: two pickers per row under "photos" with captions aligned
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?,?,0)", (1, "2026-02-03"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-02-03'").fetchone()["id"]
    c.commit()
    c.close()

    files = [
        ("photos", ("", b"", "application/octet-stream")),
        ("photos", ("cam.png", b"cam", "image/png")),
        ("photos", ("gal.png", b"gal", "image/png")),
        ("photos", ("", b"", "application/octet-stream")),
    ]
    data = {"date": "2026-02-03", "photo_captions": ["cap-cam", "cap-gal"]}
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rows = _new_rows(did)
    assert len(rows) == 2
    for row in rows:
        data_bytes = (TEST_UPLOAD / row["filename"]).read_bytes() if (TEST_UPLOAD / row["filename"]).exists() else None
        if data_bytes == b"cam":
            assert row["caption"] == "cap-cam"
        if data_bytes == b"gal":
            assert row["caption"] == "cap-gal"

    c = connect()
    c.execute("DELETE FROM diary_photos WHERE diary_id=?", (did,))
    c.execute("DELETE FROM diaries WHERE id=?", (did,))
    c.commit()
    c.close()
