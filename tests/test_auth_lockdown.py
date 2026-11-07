"""TDD: backend authorization lock-down (Task 1).

RED phase — every test here must FAIL against the current codebase until the
project-access / role checks are enforced server-side on every protected route.
"""
import os
import sys
import tempfile
import hashlib
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

ROLES = ["viewer", "supervisor", "investor", "admin"]
ROLE_UID = {"viewer": 1, "supervisor": 2, "investor": 3, "admin": 4}


def _token(uid, role):
    return create_access_token({"user_id": uid, "role": role})


def _client_with(tc, token):
    tc.cookies["access_token"] = token
    return tc


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
    c.execute("INSERT INTO projects (name) VALUES (?)", ("Other Project",))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("viewer", "x", "Viewer", "viewer"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("supervisor", "x", "Supervisor", "supervisor"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("investor", "x", "Investor", "investor"))
    c.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
              ("admin", "x", "Admin", "admin"))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (?,?)", (1, 1))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (?,?)", (2, 1))
    c.execute("INSERT OR IGNORE INTO user_projects (user_id, project_id) VALUES (?,?)", (3, 1))
    c.commit()
    c.close()
    with TestClient(mainmod.app) as tc:
        _client_with(tc, _token(1, "viewer"))
        yield tc


@pytest.fixture(autouse=True)
def _reset():
    r2mod._client = None
    r2mod._quota = None
    yield
    r2mod._client = None
    r2mod._quota = None


def _row(table, where, params=()):
    c = connect()
    try:
        r = c.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


_DATES = {"n": 1000}


def _date():
    _DATES["n"] += 1
    return "2026-0%02d" % (_DATES["n"] % 28 + 1)


def _seed_diary(pid=1):
    d = _date()
    c = connect()
    c.execute("INSERT INTO diaries (project_id, date, total_workers) VALUES (?, ?, 0)",
              (pid, d))
    did = c.execute(
        "SELECT id FROM diaries WHERE project_id=? AND date=?", (pid, d)
    ).fetchone()["id"]
    c.commit()
    c.close()
    return did


def _tc(role):
    tc = TestClient(mainmod.app)
    _client_with(tc, _token(ROLE_UID[role], role))
    return tc

# 6. Photo/media routes
def test_upload_route_requires_project_access(client):
    # viewer is assigned to project 1 only; project 2 has no assignment.
    # A write to project 2 must be denied with 403 (not 302, since the
    # caller IS authenticated — a 302 here would mean the write succeeded).
    r = client.post("/projects/2/diary/save", data={"date": "2026-01-03"},
                    follow_redirects=False)
    assert r.status_code == 403
    # Side effect: no diary row must be created on project 2.
    assert _row("diaries", "project_id=2 AND date='2026-01-03'") is None


def test_serve_upload_requires_auth():
    # Use a FRESH client so the module-scoped fixture cookie is not
    # destroyed: popping it from the shared jar left every later test in
    # the module running unauthenticated and passing on 302 alone.
    tc = TestClient(mainmod.app)
    assert tc.get("/uploads/somefile.jpg",
                  follow_redirects=False).status_code in (302, 401, 403, 404)


# 7. PDF export/preview
def test_diary_preview_requires_project_access(client):
    # viewer is assigned to project 1 only; the diary lives on project 2.
    # Denial must be 403 (authenticated caller), never 200.
    did = _seed_diary(pid=2)
    r = client.get(f"/projects/2/diary/{did}/preview", follow_redirects=False)
    assert r.status_code == 403


def test_diary_pdf_requires_project_access(client):
    did = _seed_diary(pid=2)
    r = client.get(f"/projects/2/diary/{did}/pdf", follow_redirects=False)
    assert r.status_code == 403


def test_diary_preview_mismatched_project_returns_404(client):
    """F1: the path project_id must match the diary's owning project.

    viewer is assigned to project 1 and owns a diary there; pointing the
    URL at the unassigned project 2 must not leak project 2's metadata.
    """
    did = _seed_diary(pid=1)
    r = client.get(f"/projects/2/diary/{did}/preview", follow_redirects=False)
    assert r.status_code == 404
    assert "Other Project" not in r.text


def test_diary_pdf_mismatched_project_returns_404(client):
    did = _seed_diary(pid=1)
    r = client.get(f"/projects/2/diary/{did}/pdf", follow_redirects=False)
    assert r.status_code == 404


# 8. Telegram send
def test_telegram_send_requires_project_access(client):
    did = _seed_diary(pid=2)
    r = client.post(f"/projects/2/diary/{did}/send-telegram",
                    follow_redirects=False)
    assert r.status_code == 403


# 9. Read-only roles cannot edit/delete
@pytest.mark.parametrize("role", ["viewer", "supervisor", "investor"])
def test_readonly_role_cannot_delete_diary(client, role):
    did = _seed_diary(pid=1)
    r = _tc(role).post(f"/projects/1/diary/{did}/delete", follow_redirects=False)
    assert r.status_code == 403
    assert _row("diaries", "id=?", (did,)) is not None


@pytest.mark.parametrize("role", ["viewer", "supervisor", "investor"])
def test_readonly_role_cannot_create_diary(role):
    # A POST that returns 302 would mean the diary was created — the route
    # redirects on success — so denial must be 403 and the row must be
    # absent.
    r = _tc(role).post("/projects/1/diary/save", data={"date": "2026-01-04"},
                      follow_redirects=False)
    assert r.status_code == 403
    assert _row("diaries", "project_id=1 AND date='2026-01-04'") is None


@pytest.mark.parametrize("role", ["viewer", "supervisor", "investor"])
def test_readonly_role_cannot_send_telegram(role):
    did = _seed_diary(pid=1)
    r = _tc(role).post(f"/projects/1/diary/{did}/send-telegram",
                       follow_redirects=False)
    assert r.status_code == 403


# 10. Admin-only routes
def test_non_admin_cannot_access_admin_issues(client):
    # /admin/issues now carries require_role("admin"); a viewer must get 403,
    # never a 302 (a 302 would mean the route let them through to login and
    # then rendered -- i.e. no server-side gate).
    assert client.get("/admin/issues",
                      follow_redirects=False).status_code == 403
    assert _tc("admin").get("/admin/issues",
                            follow_redirects=False).status_code == 200


def test_admin_can_access_admin_issues():
    assert _tc("admin").get("/admin/issues",
                            follow_redirects=False).status_code == 200


def test_non_admin_cannot_access_admin_settings(client):
    assert client.get("/admin/settings",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_access_admin_users(client):
    assert client.get("/admin/users",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_access_admin_library(client):
    assert client.get("/admin/library",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_access_admin_storage(client):
    assert client.get("/admin/storage",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_access_admin_reminders(client):
    assert client.get("/admin/reminders",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_create_project(client):
    assert client.post("/projects/new", data={"name": "X"},
                       follow_redirects=False).status_code == 403
    assert _row("projects", "name='X'") is None


def test_non_admin_cannot_delete_project(client):
    assert client.post("/projects/1/delete",
                       follow_redirects=False).status_code == 403
    assert _row("projects", "id=1") is not None


def test_non_admin_cannot_access_reminders_run_api(client):
    assert client.get("/api/reminders/run",
                      follow_redirects=False).status_code == 403


def test_non_admin_cannot_access_reminders_status_api(client):
    assert client.get("/api/reminders/status",
                      follow_redirects=False).status_code == 403


# 11. Admin bypasses assignment
def test_admin_can_access_unassigned_project_detail():
    assert _tc("admin").get("/projects/2",
                            follow_redirects=False).status_code == 200


def test_admin_can_save_diary_on_unassigned_project():
    r = _tc("admin").post("/projects/2/diary/save", data={"date": "2026-01-05"},
                         follow_redirects=False)
    assert r.status_code == 302


# 12. Positive reads -- assigned read-only roles CAN read (F9 acceptance gap)
@pytest.mark.parametrize("role", ["viewer", "supervisor", "investor"])
def test_readonly_role_can_read_assigned_project(role):
    r = _tc(role).get("/projects/1", follow_redirects=False)
    assert r.status_code == 200, f"{role} on assigned project 1: {r.status_code}"
    assert "Test Project" in r.text


@pytest.mark.parametrize("role", ["viewer", "supervisor", "investor"])
def test_readonly_role_can_read_assigned_diary_preview(role):
    did = _seed_diary(pid=1)
    r = _tc(role).get(f"/projects/1/diary/{did}/preview", follow_redirects=False)
    assert r.status_code == 200, f"{role} preview: {r.status_code}"


def test_admin_can_read_unassigned_project_detail():
    # admin bypasses assignment -- project 2 has no members at all
    r = _tc("admin").get("/projects/2", follow_redirects=False)
    assert r.status_code == 200
    assert "Other Project" in r.text


def test_unassigned_role_cannot_read_unassigned_project(client):
    # viewer is assigned to project 1 only; project 2 must be 403
    assert client.get("/projects/2", follow_redirects=False).status_code == 403


def test_unauthenticated_redirects_to_login():
    tc = TestClient(mainmod.app)
    r = tc.get("/projects/1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
