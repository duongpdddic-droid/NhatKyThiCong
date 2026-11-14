"""Weather temperature must be a whole number end to end.

Regression for the 2026-11 report: OpenWeather returns values like 31.44, the
new-diary form prefilled ``value="31.44"`` into a ``step="0.1"`` input (HTML5
constraint violation, so the browser refused to save), and a decimal typed by
the operator was stored as-is (31.7) instead of being rounded.
"""
import os
import re
import sys
import tempfile
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
DB_PATH = TMP / "nhatky_weather_int.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import create_access_token
from app.db import connect
from app.main import _round_temp

API_DATE = "2026-07-01"
SAVE_DATE = "2026-07-02"


@pytest.fixture(scope="module")
def ctx():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("INSERT INTO projects (name, lat, lon) VALUES (?,?,?)", ("Công trình W", 21.0, 105.8))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("wadmin", "x", "W Admin", "admin"))
    admin_id = c.execute("SELECT id FROM users WHERE username='wadmin'").fetchone()["id"]
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (?,1)", (admin_id,))
    # OpenWeather-style value with more than one decimal place.
    c.execute("INSERT INTO project_weather_log (project_id, log_date, session, temp, description, icon) "
              "VALUES (?,?,?,?,?,?)", (1, API_DATE, "morning", 31.44, "nắng nhẹ", "01d"))
    # A diary saved by the previous release keeps its decimal value.
    c.execute("INSERT INTO diaries (project_id, date, weather_temp, weather_desc, foreman, total_workers) "
              "VALUES (?,?,?,?,?,?)", (1, "2026-07-03", 30.5, "mây", "CHT", 0))
    legacy_id = c.execute("SELECT id FROM diaries WHERE date='2026-07-03'").fetchone()["id"]
    # Legacy diary saved without any temperature (NULL) must still be editable.
    c.execute("INSERT INTO diaries (project_id, date, weather_temp, weather_desc, foreman, total_workers) "
              "VALUES (?,?,?,?,?,?)", (1, "2026-07-04", None, "", "CHT", 0))
    null_id = c.execute("SELECT id FROM diaries WHERE date='2026-07-04'").fetchone()["id"]
    c.commit()
    c.close()

    tc = TestClient(mainmod.app)
    tc.cookies["access_token"] = create_access_token({"user_id": admin_id, "role": "admin"})
    try:
        yield {"client": tc, "legacy_id": legacy_id, "null_temp_id": null_id}
    finally:
        tc.close()


def _weather_input(html):
    m = re.search(r"<input[^>]*id=\"weather_temp\"[^>]*>", html)
    assert m, "weather_temp input missing"
    return m.group(0)


def test_round_temp_is_half_up():
    assert _round_temp(31.44) == 31
    assert _round_temp(31.5) == 32
    assert _round_temp(26.5) == 27
    assert _round_temp(-3.5) == -4
    assert _round_temp("28") == 28
    assert _round_temp(None) is None
    assert _round_temp("") is None
    assert _round_temp("abc") is None


def test_weather_input_accepts_whole_degrees_only(ctx):
    tag = _weather_input(ctx["client"].get("/projects/1/diary/new").text)
    assert 'step="1"' in tag
    assert 'step="0.1"' not in tag


def test_prefilled_weather_is_rounded(ctx):
    html = ctx["client"].get(f"/projects/1/diary/new?date_str={API_DATE}").text
    tag = _weather_input(html)
    assert 'value="31"' in tag, tag
    assert "31.44" not in tag


def test_saved_weather_is_rounded(ctx):
    client = ctx["client"]
    r = client.post("/projects/1/diary/save",
                    data={"date": SAVE_DATE, "weather_temp": "31.7", "weather_desc": "nắng",
                          "foreman": "CHT", "total_workers": "0", "issue_count": "0"},
                    follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    c = connect()
    diary_temp = c.execute("SELECT weather_temp FROM diaries WHERE date=?", (SAVE_DATE,)).fetchone()[0]
    morning = c.execute(
        "SELECT temp FROM project_weather_log WHERE project_id=1 AND log_date=? AND session='morning'",
        (SAVE_DATE,)).fetchone()
    c.close()
    assert int(diary_temp) == 32
    assert morning is not None and int(morning["temp"]) == 32


def test_decimal_from_the_api_does_not_block_saving(ctx):
    client = ctx["client"]
    html = client.get(f"/projects/1/diary/new?date_str={API_DATE}").text
    value = re.search(r'id="weather_temp"[^>]*value="([^"]*)"', html).group(1)
    assert value.isdigit(), f"non-integer prefill would violate step=1: {value!r}"


def test_edit_form_shows_whole_degrees_for_legacy_decimal(ctx):
    tag = _weather_input(ctx["client"].get(f"/projects/1/diary/{ctx['legacy_id']}/edit").text)
    assert 'value="31"' in tag, tag  # 30.5 rounds half-up to 31


def test_edit_form_renders_empty_temperature_for_legacy_null(ctx):
    """Regression: the edit route has no weather_default; a NULL temperature must
    still render an empty field instead of raising UndefinedError (500)."""
    r = ctx["client"].get(f"/projects/1/diary/{ctx['null_temp_id']}/edit")
    assert r.status_code == 200, r.text[:300]
    tag = _weather_input(r.text)
    assert 'value=""' in tag, tag


def test_pdf_and_telegram_formatting_is_half_up():
    from app.export_pdf import _fmt_temp
    from app.telegram_bot import _fmt_temp_deg

    assert _fmt_temp(30.5) == "31"
    assert _fmt_temp(None) == ""
    assert _fmt_temp_deg(30.5) == 31
    assert _fmt_temp_deg(31.44) == 31
    assert _fmt_temp_deg(None) == ""


def test_screens_render_whole_degrees(ctx):
    client = ctx["client"]
    listing = client.get("/projects/1").text
    assert "30°C" in listing or "31°C" in listing
    assert "30.5" not in listing

    preview = client.get(f"/projects/1/diary/{ctx['legacy_id']}/preview").text
    assert "30.5" not in preview
    assert "31°C" in preview or "30°C" in preview
