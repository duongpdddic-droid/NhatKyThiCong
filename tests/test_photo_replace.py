"""Tests for existing-photo replace / delete / caption behavior."""
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
    c.execute("DELETE FROM users WHERE username='admin'")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    # foreman (writer role, id=1) assigned to project 1 — the seeded admin
    # would otherwise own id=1 and silently bypass assignment checks.
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", hashlib.sha256("pass".encode()).hexdigest(), "Foreman", "foreman"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(app) as tc:
        tc.cookies["access_token"] = token
        yield tc


@pytest.fixture(autouse=True)
def fresh_photo(client):
    os.makedirs(str(UPLOAD_DIR), exist_ok=True)
    name = "old_photo.png"
    (UPLOAD_DIR / name).write_bytes(b"old-bytes")
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, "2026-01-01"))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date='2026-01-01'").fetchone()["id"]
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)", (did, name, "Old caption"))
    c.commit()
    pid = c.execute("SELECT id FROM diary_photos WHERE filename=?", (name,)).fetchone()["id"]
    c.close()
    yield {"pid": pid, "diary_id": did, "name": name}
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
            f.unlink()
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
    pid = fresh_photo["pid"]
    files = {f"existing_photo_replace_{pid}": ("new.png", b"new-bytes", "image/png")}
    data = {"date": "2026-01-01", "existing_photo_ids": [str(pid)],
            "existing_photo_mode": "1",
            "existing_photo_captions": ["Replaced caption"]}
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row is not None
    assert row["caption"] == "Replaced caption"
    assert row["filename"] != "old_photo.png"
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"new-bytes"
    assert not (UPLOAD_DIR / "old_photo.png").exists()


def test_replace_preserves_caption_when_none_given(client, fresh_photo):
    pid = fresh_photo["pid"]
    files = {f"existing_photo_replace_{pid}": ("new2.png", b"new2", "image/png")}
    data = {"date": "2026-01-01", "existing_photo_ids": [str(pid)],
            "existing_photo_mode": "1",
            "existing_photo_captions": [""]}
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row["filename"] != "old_photo.png"
    assert row["caption"] == "Old caption"
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"new2"


def test_delete_existing_photo_removes_record_and_file(client, fresh_photo):
    pid = fresh_photo["pid"]
    old_name = _row(pid)["filename"]
    assert (UPLOAD_DIR / old_name).exists()
    data = {"date": "2026-01-01", "existing_photo_ids": [], "existing_photo_captions": [], "existing_photo_mode": "1"}
    r = client.post("/projects/1/diary/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    assert _row(pid) is None
    assert not (UPLOAD_DIR / old_name).exists()


def test_keep_existing_photo_updates_caption_only(client, fresh_photo):
    pid = fresh_photo["pid"]
    old_name = _row(pid)["filename"]
    data = {"date": "2026-01-01", "existing_photo_ids": [str(pid)],
            "existing_photo_mode": "1",
            "existing_photo_captions": ["Updated caption"]}
    r = client.post("/projects/1/diary/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row["caption"] == "Updated caption"
    assert row["filename"] == old_name


def test_edit_save_route_handles_replace_delete_caption(client, fresh_photo):
    pid = fresh_photo["pid"]
    did = fresh_photo["diary_id"]
    old_name = _row(pid)["filename"]
    files = {f"existing_photo_replace_{pid}": ("edit_new.png", b"edit-new", "image/png")}
    data = {"date": "2026-01-01", "existing_photo_ids": [str(pid)],
            "existing_photo_mode": "1",
            "existing_photo_captions": ["Edit caption"]}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row["caption"] == "Edit caption"
    assert row["filename"] != old_name
    assert (UPLOAD_DIR / row["filename"]).read_bytes() == b"edit-new"
    assert not (UPLOAD_DIR / old_name).exists()


def test_replace_two_photos_keeps_each_correct(client, fresh_photo):
    """Regression: the template renders two replace pickers per row, so a
    flat getlist("existing_photo_replace") returns twice as many entries as
    there are photos and an index-based pairing replaces the wrong photo."""
    pid = fresh_photo["pid"]
    # Seed a second existing photo in the same diary.
    (UPLOAD_DIR / "second.png").write_bytes(b"second-bytes")
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (fresh_photo["diary_id"], "second.png", "Second caption"))
    c.commit()
    pid2 = c.execute("SELECT id FROM diary_photos WHERE filename='second.png'").fetchone()["id"]
    c.close()
    try:
        files = {
            f"existing_photo_replace_{pid}": ("rep1.png", b"rep1", "image/png"),
            f"existing_photo_replace_{pid2}": ("rep2.png", b"rep2", "image/png"),
        }
        data = {
            "date": "2026-01-01",
            "existing_photo_ids": [str(pid), str(pid2)],
            "existing_photo_mode": "1",
            "existing_photo_captions": ["One", "Two"],
        }
        r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
        assert r.status_code == 302
        r1, r2 = _row(pid), _row(pid2)
        assert r1 is not None and r2 is not None
        assert r1["caption"] == "One" and r2["caption"] == "Two"
        assert (UPLOAD_DIR / r1["filename"]).read_bytes() == b"rep1"
        assert (UPLOAD_DIR / r2["filename"]).read_bytes() == b"rep2"
        assert not (UPLOAD_DIR / "old_photo.png").exists()
        assert not (UPLOAD_DIR / "second.png").exists()
    finally:
        c = connect()
        c.execute("DELETE FROM diary_photos WHERE diary_id=?", (fresh_photo["diary_id"],))
        c.close()
        for f in UPLOAD_DIR.glob("*.png"):
            try:
                f.unlink()
            except OSError:
                pass


def test_new_photos_survive_without_existing_photo_mode(client, fresh_photo):
    """Regression: a pure create must not delete just-uploaded photos.

    The create route used to call _queue_photo_replacements unconditionally,
    which deleted every photo not listed in existing_photo_ids (empty on a
    pure create) — including the ones just inserted in the same request.
    """
    did = fresh_photo["diary_id"]
    files = [("photos", ("new_a.png", b"new-a", "image/png")),
             ("photos", ("new_b.png", b"new-b", "image/png"))]
    data = {"date": "2026-01-01"}
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rows = [dict(x) for x in connect().execute(
        "SELECT * FROM diary_photos WHERE diary_id=?", (did,)).fetchall()]
    assert len(rows) == 3
    names = {row["filename"] for row in rows}
    assert len(names) == 3
    for row in rows:
        if row["filename"] != "old_photo.png":
            assert (UPLOAD_DIR / row["filename"]).read_bytes() in (b"new-a", b"new-b")
    # The pre-existing photo must also be untouched (no existing_photo_mode).
    old = _row(fresh_photo["pid"])
    assert old is not None
    assert old["filename"] == "old_photo.png"


def test_new_and_replace_together_keeps_both(client, fresh_photo):
    """Regression: replacing an existing photo must not delete a new upload
    in the same request, and the replaced old file is removed only after the
    new one is written."""
    pid = fresh_photo["pid"]
    did = fresh_photo["diary_id"]
    old_name = _row(pid)["filename"]
    files = [
        (f"existing_photo_replace_{pid}", ("rep.png", b"rep-bytes", "image/png")),
        ("photos", ("brand_new.png", b"brand-new", "image/png")),
    ]
    data = {"date": "2026-01-01", "existing_photo_ids": [str(pid)],
            "existing_photo_captions": ["Replaced"], "existing_photo_mode": "1"}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rep = _row(pid)
    assert rep is not None
    assert rep["caption"] == "Replaced"
    assert rep["filename"] != old_name
    assert (UPLOAD_DIR / rep["filename"]).read_bytes() == b"rep-bytes"
    assert not (UPLOAD_DIR / old_name).exists()
    new_rows = [dict(x) for x in connect().execute(
        "SELECT * FROM diary_photos WHERE diary_id=? AND id!=?",
        (did, pid)).fetchall()]
    assert len(new_rows) == 1
    assert (UPLOAD_DIR / new_rows[0]["filename"]).read_bytes() == b"brand-new"


def test_new_photo_filenames_are_unique(client, fresh_photo):
    """Acceptance: multiple new uploads in one request get distinct names."""
    did = fresh_photo["diary_id"]
    files = [("photos", ("a.png", b"aaa", "image/png")),
             ("photos", ("b.png", b"bbb", "image/png")),
             ("photos", ("c.png", b"ccc", "image/png"))]
    data = {"date": "2026-01-01"}
    r = client.post("/projects/1/diary/save", data=data, files=files, follow_redirects=False)
    assert r.status_code == 302
    rows = [dict(x) for x in connect().execute(
        "SELECT filename FROM diary_photos WHERE diary_id=?", (did,)).fetchall()]
    names = [row["filename"] for row in rows]
    # old_photo.png + 3 new, all distinct
    assert "old_photo.png" in names
    new_names = [n for n in names if n != "old_photo.png"]
    assert len(new_names) == 3
    assert len(set(new_names)) == 3
    for n in new_names:
        assert (UPLOAD_DIR / n).exists()


def test_edit_save_route_deletes_existing_photo(client, fresh_photo):
    pid = fresh_photo["pid"]
    did = fresh_photo["diary_id"]
    old_name = _row(pid)["filename"]
    data = {"date": "2026-01-01", "existing_photo_ids": [], "existing_photo_captions": [], "existing_photo_mode": "1"}
    r = client.post(f"/projects/1/diary/{did}/save", data=data, follow_redirects=False)
    assert r.status_code == 302
    assert _row(pid) is None
    assert not (UPLOAD_DIR / old_name).exists()