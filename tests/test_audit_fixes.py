"""Regression tests for the bugs found in the 2026-11 app audit.

Each test pins one confirmed defect: the weekly report 500, the broken
Telegram test/send calls, unauthenticated telegram endpoints, duplicated
acceptance rows, the crash on comma-joined issue photo ids, the 500/422 error
class (user delete FK, duplicate username, missing 404s, bad ids, multi-recipient
submit) and the diary date validation.
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
import app.telegram_bot as tb

TMP = Path(tempfile.mkdtemp())
DB_PATH = TMP / "nhatky_audit.db"
TEST_UPLOAD = TMP / "uploads"
dbmod.DB_PATH = DB_PATH
dbmod.IS_PG = False
mainmod.UPLOAD_DIR = TEST_UPLOAD
r2mod.LOCAL_DIR = TEST_UPLOAD

from app.auth import create_access_token
from app.db import connect

WEEK_START = "2026-05-04"
DIARY_DATE = "2026-05-06"
OTHER_DATE = "2026-05-07"


@pytest.fixture(scope="module")
def ctx():
    dbmod.DB_PATH = DB_PATH
    r2mod.LOCAL_DIR = TEST_UPLOAD
    r2mod._client = None
    if DB_PATH.exists():
        DB_PATH.unlink()
    dbmod.init_db()
    c = connect()
    c.execute("DELETE FROM users WHERE username IN ('admin','audit-admin','site-foreman','site-viewer','ex-author')")
    c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
    c.execute("INSERT INTO projects (name, lat, lon) VALUES (?,?,?)", ("Công trình A", 21.02, 105.83))
    for username, role in (("audit-admin", "admin"), ("site-foreman", "foreman"),
                           ("site-viewer", "viewer"), ("ex-author", "foreman")):
        c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                  (username, "x", username, role))
    ids = {r["username"]: r["id"] for r in c.execute("SELECT id, username FROM users").fetchall()}
    for uid in ids.values():
        c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (?,1)", (uid,))
    c.execute("INSERT INTO diaries (project_id, date, foreman, total_workers, created_by) VALUES (1,?,?,?,?)",
              (DIARY_DATE, "CHT", 5, ids["site-foreman"]))
    diary_id = c.execute("SELECT id FROM diaries WHERE date=?", (DIARY_DATE,)).fetchone()["id"]
    c.execute("INSERT INTO diaries (project_id, date, foreman, created_by) VALUES (1,?,?,?)",
              (OTHER_DATE, "CHT", ids["ex-author"]))
    other_diary = c.execute("SELECT id FROM diaries WHERE date=?", (OTHER_DATE,)).fetchone()["id"]
    labor_id = c.execute("SELECT id FROM labor_types ORDER BY id LIMIT 1").fetchone()["id"]
    mach_id = c.execute("SELECT id FROM machinery_types ORDER BY id LIMIT 1").fetchone()["id"]
    c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
              (diary_id, labor_id, 5))
    c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
              (diary_id, mach_id, 3))
    c.execute("INSERT INTO diary_tasks (diary_id, task_name, quantity, unit, cumulative_pct) VALUES (?,?,?,?,?)",
              (diary_id, "Đổ bê tông", 12, "m3", 40))
    c.execute("INSERT INTO diary_acceptance (diary_id, item_name, result, inspector) VALUES (?,?,?,?)",
              (diary_id, "Thử áp lực", "passed", "KS-ALPHA"))
    c.execute("INSERT INTO diary_acceptance (diary_id, item_name, result, inspector) VALUES (?,?,?,?)",
              (diary_id, "Kiểm tra hàn", "passed", "KS-BETA"))
    c.execute("INSERT INTO project_weather_log (project_id, log_date, session, temp, description, icon) "
              "VALUES (1,?,?,?,?,?)", (DIARY_DATE, "morning", 30.0, "nắng nhẹ", "01d"))
    c.execute("INSERT INTO recipients (project_id, name, telegram_chat_id, telegram_type, frequency) "
              "VALUES (1,'Nhóm thi công','-100777','group','weekly')")
    # A second project nobody is assigned to, for the access-control checks.
    c.execute("INSERT INTO projects (name, lat, lon) VALUES (?,?,?)", ("Công trình B", 21.0, 105.8))
    c.commit()
    c.close()

    admin = TestClient(mainmod.app)
    admin.cookies["access_token"] = create_access_token({"user_id": ids["audit-admin"], "role": "admin"})
    anon = TestClient(mainmod.app)
    foreman = TestClient(mainmod.app)
    foreman.cookies["access_token"] = create_access_token(
        {"user_id": ids["site-foreman"], "role": "foreman"})
    viewer = TestClient(mainmod.app)
    viewer.cookies["access_token"] = create_access_token(
        {"user_id": ids["site-viewer"], "role": "viewer"})
    try:
        yield {"admin": admin, "anon": anon, "foreman": foreman, "viewer": viewer,
               "ids": ids, "diary_id": diary_id,
               "other_diary": other_diary, "labor_id": labor_id, "machinery_id": mach_id}
    finally:
        admin.close()
        anon.close()
        foreman.close()
        viewer.close()


def _count(sql, params=()):
    c = connect()
    try:
        return c.execute(sql, params).fetchone()[0]
    finally:
        c.close()


def _diary_form(**overrides):
    data = {"date": "2026-06-01", "weather_temp": "30", "weather_desc": "Nắng",
            "foreman": "CHT", "total_workers": "0", "issue_count": "0"}
    data.update(overrides)
    return data


# ── L1/L4: weekly report ─────────────────────────────────────────────────────

def test_weekly_report_page_renders(ctx):
    r = ctx["admin"].get(f"/projects/1/weekly-report?week_start={WEEK_START}")
    assert r.status_code == 200, r.text[:300]
    assert "Công trình A" in r.text
    # Aggregations must have actually run (acceptance inspectors + weather note)
    assert "KS-ALPHA" in r.text
    assert "nắng nhẹ" in r.text
    assert "Đổ bê tông" in r.text


def test_project_page_links_to_the_weekly_report(ctx):
    html = ctx["admin"].get("/projects/1").text
    assert 'href="/projects/1/weekly-report"' in html, "project page has no weekly-report entry point"
    assert "Báo cáo tuần" in html


def test_dashboard_links_to_the_weekly_report(ctx):
    html = ctx["admin"].get("/").text
    assert 'href="/projects/1/weekly-report"' in html, "dashboard has no weekly-report entry point"


def test_foreman_can_open_and_send_the_weekly_report(ctx, monkeypatch):
    sent = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True, "error": None}

    monkeypatch.setattr(tb, "send_message", fake_send)
    monkeypatch.setattr(tb, "get_bot_token", lambda: "123:TOKEN")

    r = ctx["foreman"].get(f"/projects/1/weekly-report?week_start={WEEK_START}")
    assert r.status_code == 200, r.text[:300]
    assert "Công trình A" in r.text

    r = ctx["foreman"].post("/projects/1/weekly-report/send", data={"week_start": WEEK_START})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["sent"] == 1
    assert sent and sent[0][0] == "-100777"


def test_foreman_link_is_visible_but_not_for_readers(ctx):
    foreman_html = ctx["foreman"].get("/projects/1").text
    assert 'href="/projects/1/weekly-report"' in foreman_html
    assert "Báo cáo tuần" in foreman_html

    viewer_html = ctx["viewer"].get("/projects/1").text
    assert 'href="/projects/1/weekly-report"' not in viewer_html


def test_foreman_dashboard_shows_the_report_link(ctx):
    html = ctx["foreman"].get("/").text
    assert 'href="/projects/1/weekly-report"' in html


def test_weekly_report_is_scoped_to_assigned_projects(ctx):
    # Foreman is not assigned to project 2 -> denied, not 500/200.
    assert ctx["foreman"].get("/projects/2/weekly-report").status_code == 403
    assert ctx["foreman"].post("/projects/2/weekly-report/send",
                               data={"week_start": WEEK_START}).status_code == 403


def test_readers_cannot_open_the_weekly_report(ctx):
    assert ctx["viewer"].get("/projects/1/weekly-report").status_code == 403
    assert ctx["anon"].get("/projects/1/weekly-report",
                           follow_redirects=False).status_code in (302, 401, 403)


def test_weekly_report_page_renders_for_empty_week(ctx):
    r = ctx["admin"].get("/projects/1/weekly-report?week_start=2026-01-05")
    assert r.status_code == 200


def test_weekly_report_send_calls_telegram_with_two_args(ctx, monkeypatch):
    sent = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))
        return {"ok": True, "error": None}

    monkeypatch.setattr(tb, "send_message", fake_send)
    monkeypatch.setattr(tb, "get_bot_token", lambda: "123:TOKEN")
    r = ctx["admin"].post("/projects/1/weekly-report/send", data={"week_start": WEEK_START})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["sent"] == 1, body
    assert body["ok"] is True
    assert sent and sent[0][0] == "-100777"
    assert "CÔNG TRÌNH A" in sent[0][1].upper()


# ── L2/L9: telegram test endpoint ────────────────────────────────────────────

def test_test_telegram_endpoints_require_auth(ctx):
    anon = ctx["anon"]
    r = anon.post("/api/test-telegram", data={"chat_id": "123"}, follow_redirects=False)
    assert r.status_code in (401, 403, 302), r.status_code
    r = anon.get("/api/test-telegram", follow_redirects=False)
    assert r.status_code in (401, 403, 302), r.status_code


def test_test_telegram_send_uses_two_arg_signature(ctx, monkeypatch):
    calls = []

    async def fake_send(chat_id, text):
        calls.append((chat_id, text))
        return {"ok": True, "error": None}

    monkeypatch.setattr(tb, "send_message", fake_send)
    monkeypatch.setattr(tb, "get_bot_token", lambda: "123:TOKEN")
    r = ctx["admin"].post("/api/test-telegram", data={"chat_id": "555"})
    assert r.status_code == 200
    assert r.json()["ok"] is True, r.json()
    assert calls and calls[0][0] == "555"


# ── L5: acceptance rows must not duplicate on save ──────────────────────────

def test_diary_edit_save_does_not_duplicate_acceptance(ctx):
    did = ctx["diary_id"]
    before = _count("SELECT COUNT(*) FROM diary_acceptance WHERE diary_id=?", (did,))
    form = _diary_form(date=DIARY_DATE, foreman="CHT",
                       acceptance_0_item_name="Thử áp lực", acceptance_0_result="passed",
                       acceptance_0_inspector="KS-ALPHA",
                       acceptance_1_item_name="Kiểm tra hàn", acceptance_1_result="passed",
                       acceptance_1_inspector="KS-BETA")
    r = ctx["admin"].post(f"/projects/1/diary/{did}/save", data=form, follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    after = _count("SELECT COUNT(*) FROM diary_acceptance WHERE diary_id=?", (did,))
    assert after == before == 2, f"acceptance rows duplicated: {before} -> {after}"


def test_new_diary_form_warns_when_the_date_already_has_a_diary(ctx):
    did = ctx["diary_id"]
    html = ctx["admin"].get(f"/projects/1/diary/new?date_str={DIARY_DATE}").text
    assert "đã có nhật ký" in html.lower(), "missing duplicate-date warning"
    assert f"/projects/1/diary/{did}/edit" in html, "missing link to the existing diary"
    # The form still opens so the operator can pick another date.
    assert f'value="{DIARY_DATE}"' in html


def test_new_diary_form_has_no_warning_for_a_free_date(ctx):
    html = ctx["admin"].get("/projects/1/diary/new").text
    assert "đã có nhật ký" not in html.lower()


def test_new_diary_save_refuses_to_overwrite_existing_date(ctx):
    did = ctx["diary_id"]
    before_acceptance = _count("SELECT COUNT(*) FROM diary_acceptance WHERE diary_id=?", (did,))
    before_labor = _count("SELECT COUNT(*) FROM diary_labor WHERE diary_id=?", (did,))
    r = ctx["admin"].post("/projects/1/diary/save",
                          data=_diary_form(date=DIARY_DATE, foreman="GHI ĐÈ",
                                           acceptance_0_item_name="Hạng mục đè"),
                          follow_redirects=False)
    assert r.status_code == 409, r.text[:300]
    body = r.text.lower()
    assert "đã có nhật ký" in body
    assert f"/projects/1/diary/{did}/edit" in body
    c = connect()
    row = c.execute("SELECT foreman, total_workers FROM diaries WHERE id=?", (did,)).fetchone()
    c.close()
    assert row["foreman"] == "CHT", "existing diary was overwritten"
    assert _count("SELECT COUNT(*) FROM diary_acceptance WHERE diary_id=?", (did,)) == before_acceptance
    assert _count("SELECT COUNT(*) FROM diary_labor WHERE diary_id=?", (did,)) == before_labor


def test_new_diary_save_creates_a_diary_on_a_free_date(ctx):
    r = ctx["admin"].post("/projects/1/diary/save",
                          data=_diary_form(date="2026-06-10"), follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    assert _count("SELECT COUNT(*) FROM diaries WHERE date='2026-06-10'") == 1


# ── L6: comma-joined issue photo ids ────────────────────────────────────────

def test_diary_save_accepts_comma_joined_issue_photo_ids(ctx):
    r = ctx["admin"].post("/projects/1/diary/save",
                          data=_diary_form(date="2026-06-02", issue_photo_ids="1,2"),
                          follow_redirects=False)
    assert r.status_code == 302, r.text[:300]


def test_diary_edit_save_accepts_comma_joined_issue_photo_ids(ctx):
    did = ctx["diary_id"]
    r = ctx["admin"].post(f"/projects/1/diary/{did}/save",
                          data=_diary_form(date=DIARY_DATE, issue_photo_ids="1, 2 ,x"),
                          follow_redirects=False)
    assert r.status_code == 302, r.text[:300]


# ── L11/L12: user management ────────────────────────────────────────────────

def test_delete_user_that_authored_diaries(ctx):
    ids = ctx["ids"]
    author = ids["ex-author"]
    r = ctx["admin"].post(f"/admin/users/{author}/delete", follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    assert _count("SELECT COUNT(*) FROM users WHERE id=?", (author,)) == 0
    # The diary is kept, the author reference is cleared.
    assert _count("SELECT COUNT(*) FROM diaries WHERE created_by=?", (author,)) == 0
    assert _count("SELECT COUNT(*) FROM diaries WHERE date=?", (OTHER_DATE,)) == 1


def test_create_user_with_duplicate_username_reports_error(ctx):
    before = _count("SELECT COUNT(*) FROM users WHERE username='site-viewer'")
    r = ctx["admin"].post("/admin/users/create",
                          data={"username": "site-viewer", "password": "secret1",
                                "full_name": "Trùng", "role": "foreman"},
                          follow_redirects=False)
    assert r.status_code in (200, 400), r.status_code
    assert "tồn tại" in r.text.lower() or "đã có" in r.text.lower()
    assert _count("SELECT COUNT(*) FROM users WHERE username='site-viewer'") == before


# ── L13/L14/L15: missing rows must be 404, not 500 ──────────────────────────

def test_diary_form_for_unknown_project_is_404(ctx):
    assert ctx["admin"].get("/projects/999/diary/new").status_code == 404


def test_user_projects_page_for_unknown_user_is_404(ctx):
    assert ctx["admin"].get("/admin/users/999/projects").status_code == 404


def test_reminders_save_for_unknown_project_is_404(ctx):
    r = ctx["admin"].post("/projects/999/reminders",
                          data={"reminder_enabled": "1", "reminder_time": "08:00"},
                          follow_redirects=False)
    assert r.status_code == 404


# ── L16: bad ids are ignored, not fatal ─────────────────────────────────────

def test_diary_save_ignores_non_numeric_labor_ids(ctx):
    labor_id = ctx["labor_id"]
    r = ctx["admin"].post("/projects/1/diary/save",
                          data=_diary_form(date="2026-06-03", labor_ids=f"abc,{labor_id}",
                                           **{f"labor_{labor_id}": "3"}),
                          follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    assert _count("""SELECT COUNT(*) FROM diary_labor dl JOIN diaries d ON d.id=dl.diary_id
                     WHERE d.date='2026-06-03' AND dl.count=3""") == 1


def test_diary_save_ignores_non_numeric_machinery_ids(ctx):
    mach_id = ctx["machinery_id"]
    r = ctx["admin"].post("/projects/1/diary/save",
                          data=_diary_form(date="2026-06-04", machinery_ids=f"x,{mach_id}",
                                           **{f"machinery_{mach_id}": "2"}),
                          follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    assert _count("""SELECT COUNT(*) FROM diary_machinery dm JOIN diaries d ON d.id=dm.diary_id
                     WHERE d.date='2026-06-04' AND dm.hours=2""") == 1


# ── L17: adding several recipients at once ─────────────────────────────────

def test_add_multiple_recipients_in_one_submit(ctx):
    before = _count("SELECT COUNT(*) FROM recipients WHERE project_id=1")
    # Repeated form fields, exactly as the multi-row template submits them.
    r = ctx["admin"].post("/projects/1/recipients/add", data={
        "name": ["Anh A", "Anh B", "Anh C"],
        "telegram_chat_id": ["111", "222", "333"],
        "telegram_type": ["account", "account", "group"],
        "frequency": ["daily", "daily", "weekly"],
    }, follow_redirects=False)
    assert r.status_code == 302, r.text[:300]
    after = _count("SELECT COUNT(*) FROM recipients WHERE project_id=1")
    assert after == before + 3
    c = connect()
    rows = {r["name"]: dict(r) for r in c.execute("SELECT * FROM recipients WHERE project_id=1")}
    c.close()
    assert rows["Anh C"]["telegram_type"] == "group"
    assert rows["Anh B"]["telegram_chat_id"] == "222"


# ── L7/L8/L18: diary date handling ─────────────────────────────────────────

def test_diary_save_rejects_missing_or_invalid_date(ctx):
    r = ctx["admin"].post("/projects/1/diary/save", data=_diary_form(date=""),
                          follow_redirects=False)
    assert r.status_code == 400
    r = ctx["admin"].post("/projects/1/diary/save", data=_diary_form(date="31/12/2026"),
                          follow_redirects=False)
    assert r.status_code == 400
    assert _count("SELECT COUNT(*) FROM diaries WHERE date='' OR date IS NULL") == 0


def test_diary_edit_save_rejects_date_already_used(ctx):
    did = ctx["diary_id"]
    r = ctx["admin"].post(f"/projects/1/diary/{did}/save",
                          data=_diary_form(date=OTHER_DATE), follow_redirects=False)
    assert r.status_code == 400
    assert _count("SELECT COUNT(*) FROM diaries WHERE id=? AND date=?", (did, DIARY_DATE)) == 1


def test_new_diary_form_allows_choosing_the_date(ctx):
    html = ctx["admin"].get("/projects/1/diary/new").text
    tag = re.search(r"<input[^>]*name=\"date_display\"[^>]*>", html)
    assert tag, "date_display input missing"
    assert "disabled" not in tag.group(0), "diary date cannot be changed"
