"""TDD: R2 storage quota guard for NhatKyThiCong.

RED phase — every test here must FAIL against the current codebase
(app/r2.py, app/db.py, app/main.py) until the quota guard is implemented.
"""
import os
import sys
import tempfile
import threading
import time
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

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_test.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import create_access_token
from app.db import connect


class _BytesBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


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
    # init_db seeds an admin at id=1; drop it so the viewer can own id=1
    # without clobbering the admin row that admin-scoped tests re-create.
    c.execute("DELETE FROM users WHERE username='admin'")
    # Reset the users autoincrement so the viewer INSERT reuses id=1 instead
    # of skipping it (which would make the viewer id=2 and break auth).
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Test Project",))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    # foreman is a writer role and must be assigned to project 1 so the new
    # server-side project-access + write-role checks permit diary saves.
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


@pytest.fixture(autouse=True)
def _reset_quota():
    """Every test starts from a clean quota ledger + fresh R2 mock."""
    r2mod._client = None
    r2mod._quota = None
    yield
    r2mod._client = None
    r2mod._quota = None


# ── helpers ────────────────────────────────────────────────────────────────────

def _small_jpeg():
    """Return a tiny valid JPEG bytes (4x4)."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _setup_r2_mock(used=0, cap=8_000_000_000, max_bytes=5_000_000,
                   max_photos=20, put_exc=None, delete_exc=None,
                   head_exists=True):
    """Configure the module-level quota + a fake S3 client.

    Also enables R2 routing (env vars) so save_file/delete_file go through
    the mock instead of falling back to the local filesystem.
    """
    r2mod.configure_quota(cap, max_bytes=max_bytes, max_photos=max_photos)
    r2mod.set_used_bytes(used)
    fake = MagicMock()
    fake.put_object.side_effect = put_exc
    fake.delete_object.side_effect = delete_exc
    fake.head_object.return_value = {}
    fake.list_objects_v2.return_value = {"Contents": []}
    fake.delete_object.return_value = {}
    fake.put_object.return_value = {}
    fake.get_object.return_value = {"Body": _BytesBody(b"x")}

    def _boto3_client(*a, **k):
        return fake
    env_patch = patch.dict(os.environ, {
        "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
        "R2_BUCKET": "bkt",
    })
    env_patch.start()
    boto_patch = patch("boto3.client", side_effect=_boto3_client)
    boto_patch.start()
    r2mod._client = None
    r2mod._client_instance()
    return fake, (env_patch, boto_patch)


@pytest.fixture(autouse=True)
def _stop_r2_patches():
    """Stop any env/boto patches started by _setup_r2_mock.

    Without this the R2 env vars leak into later tests (e.g. test_r2.py's
    local-fallback assertions) and the module-level boto3 client is shared.
    """
    yield
    import app.r2 as _r2
    _r2._client = None
    for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        os.environ.pop(k, None)

"""Unit tests for the quota ledger / reservation engine."""
import pytest

import app.r2 as r2mod


def test_default_cap_is_8gb():
    assert r2mod.DEFAULT_MAX_STORAGE_BYTES == 8_000_000_000


def test_configure_quota_sets_defaults():
    r2mod.configure_quota(1000, max_bytes=100, max_photos=5)
    assert r2mod._quota["cap"] == 1000
    assert r2mod._quota["max_bytes"] == 100
    assert r2mod._quota["max_photos"] == 5
    assert r2mod._quota["used"] == 0


def test_set_used_bytes_round_trip():
    r2mod.configure_quota(8_000_000_000)
    r2mod.set_used_bytes(1_500_000_000)
    assert r2mod.get_used_bytes() == 1_500_000_000


def test_can_reserve_false_when_over_cap():
    r2mod.configure_quota(1000, max_bytes=1000)
    r2mod.set_used_bytes(1000)
    ok, msg = r2mod.reserve(500, 1)
    assert ok is False
    assert "quota" in msg.lower() or "full" in msg.lower() or "đủ" in msg


def test_can_reserve_false_when_over_per_upload():
    r2mod.configure_quota(8_000_000_000, max_bytes=100)
    r2mod.set_used_bytes(0)
    ok, _ = r2mod.reserve(200, 1)
    assert ok is False


def test_can_reserve_false_when_over_photo_count():
    r2mod.configure_quota(8_000_000_000, max_bytes=1_000_000, max_photos=1)
    r2mod.set_used_bytes(0)
    ok1, _ = r2mod.reserve(100, 1)
    assert ok1 is True
    # Per-reservation sanity cap: a single reservation above the ceiling.
    ok2, _ = r2mod.reserve(100, 2)
    assert ok2 is False


def test_reserve_increments_used_and_release_decrements():
    r2mod.configure_quota(8_000_000_000, max_bytes=1_000_000, max_photos=10)
    r2mod.set_used_bytes(0)
    ok, _ = r2mod.reserve(300, 1)
    assert ok is True
    assert r2mod.get_used_bytes() == 300
    r2mod.release(300)
    assert r2mod.get_used_bytes() == 0


def test_concurrent_reservations_do_not_exceed_cap():
    r2mod.configure_quota(1000, max_bytes=1000, max_photos=100)
    r2mod.set_used_bytes(0)
    results = []
    errors = []

    def worker():
        try:
            ok, _ = r2mod.reserve(200, 1)
            results.append(ok)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(results) == 5
    assert r2mod.get_used_bytes() == 1000


def test_release_only_releases_reserved_amount():
    r2mod.configure_quota(8_000_000_000, max_bytes=1_000_000, max_photos=10)
    r2mod.set_used_bytes(0)
    r2mod.reserve(400, 1)
    r2mod.release(400)
    assert r2mod.get_used_bytes() == 0
    r2mod.release(999)
    assert r2mod.get_used_bytes() == 0

# ── Integration tests via the FastAPI client ────────────────────────────────────

def _row(pid):
    c = connect()
    try:
        r = c.execute("SELECT * FROM diary_photos WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


_next_date = {"d": 0}


def _fresh_date():
    """Return a unique, valid diary date per test so UNIQUE constraints never clash."""
    _next_date["d"] += 1
    from datetime import date, timedelta
    return (date(2026, 1, 1) + timedelta(days=_next_date["d"])).isoformat()


def _seed_diary(client, date=None):
    c = connect()
    d = date or _fresh_date()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)",
              (1, d))
    did = c.execute(
        "SELECT id FROM diaries WHERE project_id=1 AND date=?", (d,)
    ).fetchone()["id"]
    c.commit()
    c.close()
    return did, d


def _upload(client, data, files=None):
    return client.post("/projects/1/diary/save", data=data, files=files or {},
                       follow_redirects=False)


def test_rejected_over_cap_leaves_no_db_row_or_object(client):
    jpeg = _small_jpeg()
    _setup_r2_mock(used=8_000_000_000)
    did, _ = _seed_diary(client)
    before = connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"]
    r = _upload(client, {"date": "2026-01-01"},
                files=[("photos", ("a.jpg", jpeg, "image/jpeg"))])
    assert r.status_code in (400, 413, 422, 302)
    after = connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"]
    assert after == before
    assert r2mod.get_used_bytes() == 8_000_000_000


def test_rejected_over_per_upload_size(client):
    jpeg = _small_jpeg()
    _setup_r2_mock(used=0, max_bytes=1)
    did, d = _seed_diary(client)
    r = _upload(client, {"date": d},
                files=[("photos", ("a.jpg", jpeg, "image/jpeg"))])
    assert r.status_code in (400, 413, 422)
    assert connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"] == 0


def test_rejected_malformed_image(client):
    _setup_r2_mock(used=0)
    did, d = _seed_diary(client)
    r = _upload(client, {"date": d},
                files=[("photos", ("bad.jpg", b"not-an-image", "image/jpeg"))])
    assert r.status_code in (400, 413, 422)
    assert connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"] == 0


def test_rejected_oversize_raw_upload(client):
    _setup_r2_mock(used=0, max_bytes=100)
    did, d = _seed_diary(client)
    r = _upload(client, {"date": d},
                files=[("photos", ("big.jpg", b"x" * 500, "image/jpeg"))])
    assert r.status_code in (400, 413, 422)
    assert connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"] == 0


def test_photo_count_limit_blocks(client):
    jpeg = _small_jpeg()
    _setup_r2_mock(used=0, max_photos=2)
    did, d = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "p1.jpg", "a"))
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "p2.jpg", "b"))
    c.commit()
    c.close()
    r = _upload(client, {"date": d},
                files=[("photos", ("a.jpg", jpeg, "image/jpeg"))])
    assert r.status_code in (400, 413, 422)
    assert connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"] == 2

def test_write_failure_releases_reservation_and_leaves_no_row(client):
    jpeg = _small_jpeg()
    fake, _ = _setup_r2_mock(used=0, put_exc=RuntimeError("boom"))
    did, d = _seed_diary(client)
    r = _upload(client, {"date": d},
                files=[("photos", ("a.jpg", jpeg, "image/jpeg"))])
    assert r.status_code in (400, 500, 502, 503)
    assert connect().execute(
        "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?", (did,)
    ).fetchone()["n"] == 0
    assert r2mod.get_used_bytes() == 0
    assert fake.put_object.called


def test_replacement_growth_reserves_first(client):
    jpeg = _small_jpeg()
    fake, _ = _setup_r2_mock(used=0, cap=10_000_000_000, max_bytes=10_000_000)
    did, d = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "old.jpg", "Old"))
    c.commit()
    pid = c.execute(
        "SELECT id FROM diary_photos WHERE filename='old.jpg'"
    ).fetchone()["id"]
    c.close()
    r2mod.set_used_bytes(100)
    r = client.post(f"/projects/1/diary/{did}/save", data={
        "date": d, "existing_photo_ids": [str(pid)],
        "existing_photo_mode": "1", "existing_photo_captions": ["New"],
    }, files={f"existing_photo_replace_{pid}": ("new.jpg", jpeg, "image/jpeg")},
        follow_redirects=False)
    assert r.status_code == 302
    row = _row(pid)
    assert row is not None
    assert row["filename"] != "old.jpg"
    # In R2 mode the new object lives in the bucket (mock), not LOCAL_DIR.
    assert fake.put_object.called
    assert any(call.kwargs.get("Key") == row["filename"]
               or (call.args and call.args[0].get("Key") == row["filename"])
               for call in fake.put_object.call_args_list)
    # The old object must be deleted after the new one is committed.
    assert fake.delete_object.called
    assert any(call.kwargs.get("Key") == "old.jpg"
               or (call.args and call.args[0].get("Key") == "old.jpg")
               for call in fake.delete_object.call_args_list)
    # The old row had no size_bytes (legacy), so the full new stored size was
    # reserved; the old reservation (100) is released only after the old
    # object is confirmed deleted, so the ledger holds both momentarily.
    assert r2mod.get_used_bytes() == 100 + int(row["size_bytes"])


def test_replacement_decrease_frees_old_bytes(client):
    jpeg = _small_jpeg()
    fake, _ = _setup_r2_mock(used=0, cap=10_000_000_000, max_bytes=10_000_000)
    did, _ = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption, size_bytes) VALUES (?,?,?,?)",
              (did, "big_old.jpg", "Old", 5000))
    c.commit()
    pid = c.execute(
        "SELECT id FROM diary_photos WHERE filename='big_old.jpg'"
    ).fetchone()["id"]
    c.close()
    r2mod.set_used_bytes(5000)
    r = client.post(f"/projects/1/diary/{did}/save", data={
        "date": "2026-01-01", "existing_photo_ids": [str(pid)],
        "existing_photo_mode": "1", "existing_photo_captions": ["Smaller"],
    }, files={f"existing_photo_replace_{pid}": ("new.jpg", jpeg, "image/jpeg")},
        follow_redirects=False)
    assert r.status_code == 302
    # No growth reserved; the old 5000 bytes are released only after the old
    # object is confirmed deleted, so the ledger returns to ~0.
    assert r2mod.get_used_bytes() <= len(jpeg)
    assert fake.delete_object.called
    assert any(call.kwargs.get("Key") == "big_old.jpg"
               or (call.args and call.args[0].get("Key") == "big_old.jpg")
               for call in fake.delete_object.call_args_list)


def test_delete_success_releases_quota(client):
    jpeg = _small_jpeg()
    fake, _ = _setup_r2_mock(used=0, cap=10_000_000_000, max_bytes=10_000_000)
    did, _ = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption, size_bytes) VALUES (?,?,?,?)",
              (did, "gone.jpg", "x", 1000))
    c.commit()
    c.close()
    (TEST_UPLOAD / "gone.jpg").write_bytes(b"y" * 1000)
    r2mod.set_used_bytes(1000)
    r = client.post(f"/projects/1/diary/{did}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert r2mod.get_used_bytes() == 0
    # R2 mode: the object lives in the bucket (mock), not LOCAL_DIR, so the
    # real check is that delete_object was called for the filename.
    assert fake.delete_object.called
    assert any(call.kwargs.get("Key") == "gone.jpg"
               or (call.args and call.args[0].get("Key") == "gone.jpg")
               for call in fake.delete_object.call_args_list)


def test_delete_failure_does_not_free_quota(client):
    jpeg = _small_jpeg()
    fake, _ = _setup_r2_mock(used=0, cap=10_000_000_000, max_bytes=10_000_000,
                             delete_exc=RuntimeError("disk down"))
    did, _ = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "keep.jpg", "x"))
    c.commit()
    c.close()
    (TEST_UPLOAD / "keep.jpg").write_bytes(b"y" * 1000)
    r2mod.set_used_bytes(1000)
    r = client.post(f"/projects/1/diary/{did}/delete", follow_redirects=False)
    assert r.status_code == 302
    assert r2mod.get_used_bytes() == 1000


def test_reconciliation_failure_is_fail_closed():
    r2mod.configure_quota(8_000_000_000)
    fake = MagicMock()
    fake.list_objects_v2.side_effect = RuntimeError("network")
    r2mod._client = None
    with patch.dict(os.environ, {"R2_ACCESS_KEY_ID": "k",
                                  "R2_SECRET_ACCESS_KEY": "s",
                                  "R2_BUCKET": "bkt"}):
        r2mod._client = None
        with patch("boto3.client", return_value=fake):
            r2mod._client_instance()
            with pytest.raises(Exception):
                r2mod.reconcile_from_inventory()

def test_migration_adds_size_bytes_column(tmp_path):
    import sqlite3
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE diary_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            diary_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            caption TEXT,
            is_previous INTEGER DEFAULT 0,
            uploaded_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    saved = dbmod.DB_PATH
    dbmod.DB_PATH = db_path
    dbmod.IS_PG = False
    try:
        dbmod.init_db()
    finally:
        dbmod.DB_PATH = saved
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(diary_photos)").fetchall()}
    conn.close()
    assert "size_bytes" in cols


def test_legacy_rows_without_size_bytes_treated_as_unknown():
    r2mod.configure_quota(8_000_000_000)
    fake = MagicMock()
    fake.list_objects_v2.return_value = {"Contents": []}
    r2mod._client = None
    with patch("boto3.client", return_value=fake):
        r2mod._client_instance()
    # Seed a legacy row without size_bytes (as pre-migration DBs look).
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)",
              (1, "2025-12-31"))
    did = c.execute(
        "SELECT id FROM diaries WHERE project_id=1 AND date='2025-12-31'"
    ).fetchone()["id"]
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "legacy.jpg", "old"))
    c.commit()
    c.close()
    used, exact = r2mod.reconcile_from_inventory()
    assert exact is False


def test_admin_reconciliation_route_requires_admin(client):
    r = client.get("/admin/storage")
    assert r.status_code in (302, 403)


def test_admin_storage_route_admin_ok():
    """Admin-only page shows storage used/limit and low-space warning."""
    import hashlib
    from app.auth import create_access_token
    c = connect()
    existing = c.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()
    if not existing:
        c.execute(
            "INSERT INTO users (username, password_hash, full_name, role) "
            "VALUES (?,?,?,?)",
            ("admin", hashlib.sha256("admin123".encode()).hexdigest(),
             "Admin", "admin"),
        )
    c.commit()
    admin_id = c.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()["id"]
    c.close()
    admin_token = create_access_token({"user_id": admin_id, "role": "admin"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = admin_token
        fake = MagicMock()
        fake.list_objects_v2.return_value = {"Contents": []}
        r2mod._client = None
        with patch("boto3.client", return_value=fake):
            r2mod._client_instance()
        r = tc.get("/admin/storage")
        assert r.status_code == 200
        body = r.text.lower()
        assert "storage" in body or "bộ nhớ" in r.text or "quota" in body or "dung lượng" in body


def test_admin_reconcile_action_updates_ledger():
    import hashlib
    from app.auth import create_access_token
    c = connect()
    existing = c.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()
    if not existing:
        c.execute(
            "INSERT INTO users (username, password_hash, full_name, role) "
            "VALUES (?,?,?,?)",
            ("admin", hashlib.sha256("admin123".encode()).hexdigest(),
             "Admin", "admin"),
        )
    c.commit()
    admin_id = c.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()["id"]
    c.close()
    admin_token = create_access_token({"user_id": admin_id, "role": "admin"})
    r2mod.configure_quota(8_000_000_000)
    r2mod.set_used_bytes(0)
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = admin_token
        fake = MagicMock()
        fake.list_objects_v2.return_value = {"Contents": [
            {"Key": "a.jpg", "Size": 100},
            {"Key": "b.jpg", "Size": 200},
        ]}
        r2mod._client = None
        with patch.dict(os.environ, {"R2_ACCESS_KEY_ID": "k",
                                      "R2_SECRET_ACCESS_KEY": "s",
                                      "R2_BUCKET": "bkt"}), \
             patch("boto3.client", return_value=fake):
            r2mod._client_instance()
            r = tc.post("/admin/storage/reconcile")
            assert r.status_code in (200, 302)
            assert r2mod.get_used_bytes() == 300

