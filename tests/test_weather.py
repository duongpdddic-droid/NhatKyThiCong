"""Weather persistence and diary auto-fill regressions."""
import os
import sqlite3
import sys
import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.db as dbmod
from app.weather import ensure_weather, get_weather


def test_weather_migration_adds_location_and_log(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE diaries (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, date TEXT,
                              weather_temp REAL, weather_desc TEXT, issues TEXT, issue_level TEXT,
                              issue_status TEXT, cause TEXT, related_party TEXT,
                              workflow_status TEXT, resolved_at TEXT, issue_notified_at TEXT);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                            password_hash TEXT NOT NULL, full_name TEXT NOT NULL, role TEXT NOT NULL);
        CREATE TABLE labor_types (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, unit TEXT, sort_order INTEGER);
        CREATE TABLE machinery_types (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, unit TEXT, sort_order INTEGER);
        CREATE TABLE diary_photos (id INTEGER PRIMARY KEY AUTOINCREMENT, diary_id INTEGER, filename TEXT, caption TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO projects(name) VALUES ('P1');
    """)
    conn.commit(); conn.close()
    dbmod.DB_PATH = db_path; dbmod.IS_PG = False
    dbmod.init_db()
    conn = sqlite3.connect(db_path)
    project_cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    log_cols = {r[1] for r in conn.execute("PRAGMA table_info(project_weather_log)")}
    assert {"lat", "lon"} <= project_cols
    assert {"project_id", "log_date", "session", "temp", "description", "icon", "fetched_at"} <= log_cols
    assert conn.execute("SELECT lat, lon FROM projects WHERE id=1").fetchone() == (21.0, 105.8)
    conn.close()


def test_ensure_weather_fetches_and_stores_current_session(tmp_path):
    async def run():
        dbmod.DB_PATH = tmp_path / "weather.db"; dbmod.IS_PG = False
        dbmod.init_db()
        c = dbmod.connect()
        c.execute("INSERT INTO projects(name, lat, lon) VALUES (?,?,?)", ("P", 10.1, 106.2)); c.commit()
        with patch("app.weather.get_weather", new=AsyncMock(return_value={"temp": 31.5, "desc": "nắng", "icon": "01d"})) as fetch:
            result = await ensure_weather(c, 1, date.today().isoformat(), 10.1, 106.2, "morning")
        row = c.execute("SELECT * FROM project_weather_log WHERE project_id=1").fetchone()
        assert result["temp"] == 31.5
        assert row["session"] == "morning" and row["description"] == "nắng"
        fetch.assert_awaited_once_with(10.1, 106.2)
        c.close()
    asyncio.run(run())


def test_missing_api_key_is_safe_and_not_stored_as_weather(tmp_path):
    async def run():
        with patch.dict(os.environ, {}, clear=True):
            return await get_weather(21.0, 105.8)
    result = asyncio.run(run())
    assert result["temp"] is None
    assert "API key" in result["desc"]
