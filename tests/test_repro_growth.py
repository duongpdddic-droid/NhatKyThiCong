"""Temporary repro for test_replacement_growth_reserves_first — runs under pytest."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

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


def _small_jpeg():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def _setup_r2_mock(used=0, cap=8_000_000_000, max_bytes=5_000_000,
                   max_photos=20, put_exc=None, delete_exc=None):
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


_next_date = {"d": 0}


def _fresh_date():
    _next_date["d"] += 1
    return "2026-01-%02d" % _next_date["d"]


def _seed_diary(client, date=None):
    c = connect()
    d = date or _fresh_date()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)", (1, d))
    did = c.execute("SELECT id FROM diaries WHERE project_id=1 AND date=?", (d,)).fetchone()["id"]
    c.commit()
    c.close()
    return did, d


def _row(pid):
    c = connect()
    try:
        r = c.execute("SELECT * FROM diary_photos WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


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
    # foreman (writer role, id=1) assigned to project 1 — the seeded admin
    # would otherwise own id=1 and silently bypass assignment checks.
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (1, 1)")
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "foreman"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


@pytest.fixture(autouse=True)
def _reset_quota():
    r2mod._client = None
    r2mod._quota = None
    yield
    r2mod._client = None
    r2mod._quota = None


def test_repro_growth(client, capsys):
    orig_reserve = r2mod.reserve
    orig_release = r2mod.release
    orig_persist = mainmod._persist_photo
    orig_cleanup = mainmod._cleanup_replaced_files
    orig_queue = mainmod._queue_photo_replacements

    def traced_reserve(bc, pc=1):
        before = r2mod.get_used_bytes()
        ok, msg = orig_reserve(bc, pc)
        after = r2mod.get_used_bytes()
        print(f"  reserve({bc},{pc}) before={before} ok={ok} after={after} msg={msg!r}")
        return ok, msg

    def traced_release(bc):
        before = r2mod.get_used_bytes()
        orig_release(bc)
        after = r2mod.get_used_bytes()
        print(f"  release({bc}) before={before} after={after}")

    async def traced_persist(file_obj, diary_id, stamp):
        r = await orig_persist(file_obj, diary_id, stamp)
        print(f"  _persist_photo -> {r}")
        return r

    def traced_cleanup(deferred_old):
        print(f"  _cleanup_replaced_files deferred_old={deferred_old}")
        orig_cleanup(deferred_old)

    async def traced_queue(*a, **k):
        r = await orig_queue(*a, **k)
        print(f"  _queue_photo_replacements deferred_old={r}")
        return r

    r2mod.reserve = traced_reserve
    r2mod.release = traced_release
    mainmod._persist_photo = traced_persist
    mainmod._cleanup_replaced_files = traced_cleanup
    mainmod._queue_photo_replacements = traced_queue

    jpeg = _small_jpeg()
    print(f"jpeg len={len(jpeg)}")
    fake, _ = _setup_r2_mock(used=0, cap=10_000_000_000, max_bytes=10_000_000)
    did, d = _seed_diary(client)
    c = connect()
    c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
              (did, "old.jpg", "Old"))
    c.commit()
    pid = c.execute("SELECT id FROM diary_photos WHERE filename='old.jpg'").fetchone()["id"]
    c.close()
    r2mod.set_used_bytes(100)
    print(f"--- before request used={r2mod.get_used_bytes()} pid={pid}")
    r = client.post(f"/projects/1/diary/{did}/save", data={
        "date": d, "existing_photo_ids": [str(pid)],
        "existing_photo_mode": "1", "existing_photo_captions": ["New"],
    }, files={f"existing_photo_replace_{pid}": ("new.jpg", jpeg, "image/jpeg")},
        follow_redirects=False)
    print(f"status={r.status_code}")
    row = _row(pid)
    print(f"row={row}")
    print(f"--- after request used={r2mod.get_used_bytes()}")
    print(f"put_object calls={fake.put_object.call_args_list}")
    print(f"delete_object calls={fake.delete_object.call_args_list}")