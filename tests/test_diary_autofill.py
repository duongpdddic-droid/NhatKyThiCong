"""Regression: diary form must auto-fill weather, labor and machinery.

Root-cause guard for the 2026-11 incident where a JavaScript syntax error in
``diary_form.html`` aborted the whole inline script, so neither the previous-day
labor/machinery rows nor the weather API call ever ran.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
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
DB_PATH = TMP / "nhatky_autofill.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import create_access_token
from app.db import connect

PREV_DATE = "2026-05-01"
TARGET_DATE = "2026-05-02"
NODE = shutil.which("node")


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
    c.execute("INSERT INTO projects (name, lat, lon) VALUES (?,?,?)",
              ("Autofill Project", 10.5, 106.7))
    project_id = c.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]
    # Writer role with its own id (not the seeded admin) assigned to the project.
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("foreman", "x", "Foreman", "foreman"))
    user_id = c.execute("SELECT id FROM users WHERE username='foreman'").fetchone()["id"]
    c.execute("INSERT INTO user_projects (user_id, project_id) VALUES (?,?)",
              (user_id, project_id))
    labor_id = c.execute("SELECT id FROM labor_types ORDER BY id LIMIT 1").fetchone()["id"]
    machinery_id = c.execute("SELECT id FROM machinery_types ORDER BY id LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO diaries (project_id, date, foreman, total_workers) VALUES (?,?,?,?)",
              (project_id, PREV_DATE, "CHT A", 12))
    diary_id = c.execute("SELECT id FROM diaries ORDER BY id DESC LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
              (diary_id, labor_id, 7))
    c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
              (diary_id, machinery_id, 3))
    c.commit()
    c.close()
    token = create_access_token({"user_id": user_id, "role": "foreman"})
    # A bare TestClient (no ``with``) does not run the app lifespan, so this
    # module neither starts the background reminder thread nor leaves global DB
    # connections behind for other test modules.
    tc = TestClient(mainmod.app)
    tc.cookies["access_token"] = token
    try:
        yield tc, {
            "project_id": project_id,
            "diary_id": diary_id,
            "labor_id": labor_id,
            "machinery_id": machinery_id,
        }
    finally:
        tc.close()


def _new_diary_html(tc, project_id, diary_date=TARGET_DATE):
    r = tc.get(f"/projects/{project_id}/diary/new?date_str={diary_date}")
    assert r.status_code == 200
    return r.text


def _inline_scripts(html):
    return [s for s in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.S) if s.strip()]


def _js_value(html, name):
    """Extract the JSON value assigned to ``const <name> = ...;``."""
    m = re.search(r"const\s+%s\s*=\s*(\{.*?\});" % re.escape(name), html, re.S)
    assert m, f"{name} is not rendered in the diary form"
    return json.loads(m.group(1))


@pytest.mark.skipif(not NODE, reason="node is required to syntax-check inline JS")
def test_rendered_diary_form_inline_scripts_parse(client):
    """A syntax error in the inline script kills every autofill handler."""
    tc, ctx = client
    html = _new_diary_html(tc, ctx["project_id"])
    chunks = _inline_scripts(html)
    assert chunks
    for index, js in enumerate(chunks):
        target = TMP / f"diary_form_chunk_{index}.js"
        target.write_text(js, encoding="utf-8")
        proc = subprocess.run([NODE, "--check", str(target)],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        assert proc.returncode == 0, (
            f"inline script #{index} has a syntax error:\n{proc.stdout}{proc.stderr}")


def test_new_diary_prefills_previous_day_labor_and_machinery(client):
    tc, ctx = client
    html = _new_diary_html(tc, ctx["project_id"])
    assert _js_value(html, "prevLabor") == {str(ctx["labor_id"]): 7}
    assert _js_value(html, "prevMachinery") == {str(ctx["machinery_id"]): 3}
    # The renderer must be wired to run and to refresh the auto total.
    assert "renderPreviousData()" in html
    assert "calcWorkers()" in html


def test_new_diary_prefills_cached_weather(client):
    tc, ctx = client
    today = date.today().isoformat()
    c = connect()
    c.execute("INSERT OR REPLACE INTO project_weather_log "
              "(project_id, log_date, session, temp, description, icon) VALUES (?,?,?,?,?,?)",
              (ctx["project_id"], today, "morning", 26.5, "nắng nhẹ", "01d"))
    c.commit()
    c.close()
    html = _new_diary_html(tc, ctx["project_id"], today)
    # Weather is prefilled as a whole degree (26.5 -> 27), never as a decimal.
    assert re.search(r'id="weather_temp"[^>]*value="27"', html), "weather_temp not auto-filled"
    assert re.search(r'id="weather_desc"[^>]*value="nắng nhẹ"', html), "weather_desc not auto-filled"


def test_weather_api_reads_project_cache(client):
    tc, ctx = client
    c = connect()
    c.execute("INSERT OR REPLACE INTO project_weather_log "
              "(project_id, log_date, session, temp, description, icon) VALUES (?,?,?,?,?,?)",
              (ctx["project_id"], TARGET_DATE, "morning", 24.0, "mưa nhỏ", "10d"))
    c.commit()
    c.close()
    data = tc.get(f"/api/weather?project_id={ctx['project_id']}&date={TARGET_DATE}").json()
    assert data["temp"] == 24.0
    assert data["desc"] == "mưa nhỏ"


def test_weather_api_keeps_project_cache_when_browser_sends_other_coords(client):
    """Geolocation coordinates never match the project exactly, so the cache
    lookup must key on project_id instead of float lat/lon equality."""
    tc, ctx = client
    data = tc.get(
        f"/api/weather?project_id={ctx['project_id']}&lat=16.06&lon=108.22&date={TARGET_DATE}"
    ).json()
    assert data["temp"] == 24.0


def test_weather_api_today_without_key_reports_configuration(client, monkeypatch):
    tc, ctx = client
    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    today = date.today().isoformat()
    c = connect()
    c.execute("DELETE FROM project_weather_log WHERE project_id=? AND log_date=?",
              (ctx["project_id"], today))
    c.commit()
    c.close()
    data = tc.get(f"/api/weather?project_id={ctx['project_id']}&date={today}").json()
    assert data["temp"] is None
    assert "API key" in data["desc"]
