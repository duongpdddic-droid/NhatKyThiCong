"""Tests for recipient telegram_type support."""
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
    c.commit()
    c.close()
    token = create_access_token({"user_id": 1, "role": "admin"})
    with TestClient(mainmod.app) as tc:
        tc.cookies["access_token"] = token
        yield tc


def test_schema_has_telegram_type_default():
    dbmod.DB_PATH = DB_PATH
    dbmod.IS_PG = False
    dbmod.init_db()
    c = connect()
    cols = {r[1] for r in c.execute("PRAGMA table_info(recipients)").fetchall()}
    c.close()
    assert "telegram_type" in cols


def test_add_recipient_defaults_account(client):
    r = client.post("/projects/1/recipients/add", data={
        "name": "Alice", "telegram_chat_id": "12345", "frequency": "daily",
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    row = dict(c.execute("SELECT * FROM recipients WHERE name='Alice'").fetchone())
    c.close()
    assert row["telegram_type"] == "account"
    assert row["telegram_chat_id"] == "12345"


def test_add_recipient_group(client):
    r = client.post("/projects/1/recipients/add", data={
        "name": "DevGroup", "telegram_chat_id": "-100999", "telegram_type": "group", "frequency": "daily",
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    row = dict(c.execute("SELECT * FROM recipients WHERE name='DevGroup'").fetchone())
    c.close()
    assert row["telegram_type"] == "group"
    assert row["telegram_chat_id"] == "-100999"


def test_add_recipient_username_and_invalid_type_fallback(client):
    r = client.post("/projects/1/recipients/add", data={
        "name": "Pub", "telegram_chat_id": "@publicchan", "telegram_type": "bogus", "frequency": "daily",
    }, follow_redirects=False)
    assert r.status_code == 302
    c = connect()
    row = dict(c.execute("SELECT * FROM recipients WHERE name='Pub'").fetchone())
    c.close()
    assert row["telegram_type"] == "account"
    assert row["telegram_chat_id"] == "@publicchan"


def test_project_detail_renders_type_label(client):
    r = client.get("/projects/1")
    assert r.status_code == 200
    assert "Nhóm" in r.text or "Tài khoản" in r.text


def test_migration_existing_rows_default_account(tmp_path):
    import sqlite3
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        name TEXT NOT NULL,
        telegram_chat_id TEXT
    )""")
    conn.execute("INSERT INTO recipients (project_id, name, telegram_chat_id) VALUES (1,'Legacy','999')")
    conn.commit()
    conn.close()

    dbmod.DB_PATH = db_path
    dbmod.IS_PG = False
    dbmod.init_db()
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recipients)").fetchall()}
    val = conn.execute("SELECT telegram_type FROM recipients WHERE name='Legacy'").fetchone()[0]
    conn.close()
    assert "telegram_type" in cols
    assert val == "account"
