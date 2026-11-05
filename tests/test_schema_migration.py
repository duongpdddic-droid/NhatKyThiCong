"""Tests for backward-compatible reminder schema migration."""
import os, sys, tempfile
from pathlib import Path
import sqlite3

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.db as dbmod


def test_migration_adds_reminder_columns_to_existing_db(tmp_path):
    """Simulate an old database without reminder columns; init_db must add them."""
    db_path = tmp_path / "old.db"
    # Build an "old" projects table without reminder columns.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        address TEXT
    )""")
    conn.execute("INSERT INTO projects (name, address) VALUES ('P1','A')")
    conn.commit()
    conn.close()

    dbmod.DB_PATH = db_path
    dbmod.IS_PG = False
    dbmod.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    conn.close()
    assert "reminder_enabled" in cols
    assert "reminder_time" in cols


def test_init_db_idempotent(tmp_path):
    db_path = tmp_path / "new.db"
    dbmod.DB_PATH = db_path
    dbmod.IS_PG = False
    dbmod.init_db()
    dbmod.init_db()  # must not raise
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    conn.close()
    assert "reminder_enabled" in cols