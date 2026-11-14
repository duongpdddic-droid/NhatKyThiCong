"""Main FastAPI application — Nhật ký thi công."""
import os
import sys
import json
import re
import uuid
from pathlib import Path
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from typing import Annotated
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import connect, init_db, IS_PG
from app.auth import (
    hash_password, verify_password, create_access_token,
    password_needs_rehash,
    get_current_user, require_user, require_role,
    project_access_dep, diary_access_dep,
    project_write_dep, diary_write_dep,
)
from app import r2
from app.weather import ensure_weather, store_weather

# Debug mode - only enable stack traces in development
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    import threading
    threading.Thread(target=_reminder_loop, daemon=True).start()
    yield
    # Shutdown
    if IS_PG:
        from app.db import _pg_pool
        if _pg_pool:
            _pg_pool.closeall()


app = FastAPI(title="Nhật ký thi công", lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    print("GLOBAL ERROR:", exc)
    if DEBUG:
        print(traceback.format_exc())
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f"<pre>{traceback.format_exc()}</pre>", status_code=500)
    # Production: generic error page, no stack trace
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<h1>Internal Server Error</h1><p>An unexpected error occurred.</p>", status_code=500)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
r2.LOCAL_DIR = UPLOAD_DIR


@app.get("/uploads/{filename}")
def serve_upload(filename: str, user=Depends(require_user)):
    """Serve diary photos from R2 (private bucket) with local fallback.

    Replaces the former StaticFiles mount so the route always wins and the
    bucket never leaks a public URL.  Auth is enforced by the
    ``require_user`` dependency: unauthenticated requests are redirected to
    the login page instead of leaking private bucket contents.
    """
    data = r2.read_file(filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Ảnh không tồn tại")
    from pathlib import Path as _P
    ext = _P(filename).suffix.lower()
    media = r2.CONTENT_TYPES.get(ext, "image/jpeg")
    return Response(content=data, media_type=media)


def _reminder_loop():
    """Background scheduler: poll every 60 s and fire due reminders.

    Runs in a daemon thread so it dies with the process.  A single failed
    tick must not stop the loop.

    LIMITATION — Render free tier: the web instance is put to sleep after
    ~15 min of inactivity, so the in-process scheduler cannot fire reminders
    while the instance is asleep.  Treat this as best-effort; for guaranteed
    delivery either upgrade the plan or have an external cron hit
    /api/reminders/run (admin-authenticated) on the desired schedule.
    """
    import time
    from app.telegram_bot import _reminder_tick
    while True:
        try:
            _reminder_loop_run(_reminder_tick)
        except Exception as exc:
            print("Reminder loop error:", exc)
        time.sleep(60)


def _reminder_loop_run(tick_fn):
    """Run one tick and persist the result for the admin page."""
    import asyncio
    from app.telegram_bot import _vn_now
    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(tick_fn())
        finally:
            loop.close()
        _REMINDER_STATE["last"] = result
        _REMINDER_STATE["at"] = _vn_now().isoformat()
    except Exception as exc:
        _REMINDER_STATE["last"] = {"ok": False, "error": str(exc)}


_REMINDER_STATE = {"last": None, "at": None}


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    c = connect()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        c.close()
    if not row or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Sai tài khoản hoặc mật khẩu"})
    # Transparent upgrade: the first release stored unsalted SHA-256 digests.
    if password_needs_rehash(row["password_hash"]):
        c = connect()
        try:
            c.execute("UPDATE users SET password_hash=? WHERE id=?",
                      (hash_password(password), row["id"]))
            c.commit()
        finally:
            c.close()
    token = create_access_token({"user_id": row["id"], "role": row["role"]})
    resp = RedirectResponse(url="/", status_code=302)
    # Cookie hardening: Secure only when the browser really is on HTTPS, so a
    # plain-HTTP development server (or a proxy that terminates TLS in front of
    # the app without forwarding the scheme) does not break the login session.
    secure_cookie = request.url.scheme == "https"
    resp.set_cookie("access_token", token, httponly=True, secure=secure_cookie, samesite="lax", max_age=60*60*24*7)
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token", secure=request.url.scheme == "https", samesite="lax")
    return resp


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(require_user)):
    c = connect()
    try:
        if user["role"] == "admin":
            projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id DESC")]
        else:
            projects = [dict(r) for r in c.execute(
                """SELECT p.* FROM projects p JOIN user_projects up ON p.id=up.project_id
                   WHERE up.user_id=? ORDER BY p.id DESC""", (user["id"],))]
        # Stats
        n_diaries = c.execute("SELECT COUNT(*) AS cnt FROM diaries").fetchone()["cnt"]
        # Unresolved issue count: child table first, legacy fallback second.
        if user["role"] == "admin":
            n_issues = c.execute(
                """SELECT COUNT(*) AS cnt FROM diary_issues
                   WHERE workflow_status IN ('pending','unresolved')
                     AND description IS NOT NULL AND description != ''""").fetchone()["cnt"]
            n_issues += c.execute(
                """SELECT COUNT(*) AS cnt FROM diaries d
                   WHERE d.issues IS NOT NULL AND d.issues != ''
                     AND d.workflow_status IN ('pending','unresolved')
                     AND NOT EXISTS (SELECT 1 FROM diary_issues di
                                     WHERE di.diary_id=d.id)""").fetchone()["cnt"]
        else:
            n_issues = c.execute(
                """SELECT COUNT(*) AS cnt FROM diary_issues di
                   JOIN diaries d ON d.id=di.diary_id
                   WHERE di.workflow_status IN ('pending','unresolved')
                     AND di.description IS NOT NULL AND di.description != ''
                     AND d.project_id IN (SELECT p.id FROM projects p
                       JOIN user_projects up ON p.id=up.project_id WHERE up.user_id=?)""",
                (user["id"],)).fetchone()["cnt"]
            n_issues += c.execute(
                """SELECT COUNT(*) AS cnt FROM diaries d
                   WHERE d.issues IS NOT NULL AND d.issues != ''
                     AND d.workflow_status IN ('pending','unresolved')
                     AND NOT EXISTS (SELECT 1 FROM diary_issues di
                                     WHERE di.diary_id=d.id)
                     AND d.project_id IN (SELECT p.id FROM projects p
                       JOIN user_projects up ON p.id=up.project_id WHERE up.user_id=?)""",
                (user["id"],)).fetchone()["cnt"]
        today = date.today().isoformat()
        has_today = False
        if projects:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM diaries WHERE date=? AND project_id IN ({})".format(
                    ",".join("?" * len(projects))),
                [today] + [p["id"] for p in projects],
            ).fetchone()
            has_today = (row["n"] if row else 0) > 0
    finally:
        c.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "projects": projects, "n_diaries": n_diaries,
        "n_issues": n_issues, "has_today": has_today, "today": today,
    })


# ── Issues list ────────────────────────────────────────────────────────────────

@app.get("/admin/issues", response_class=HTMLResponse)
def issues_list(request: Request, user=Depends(require_role("admin"))):
    c = connect()
    try:
        rows = _unresolved_issue_rows(c, user)
    finally:
        c.close()
    unresolved_count = len(rows)
    return templates.TemplateResponse(request, "issues_list.html", {
        "user": user, "issues": rows,
        "unresolved_count": unresolved_count,
    })


# ── Projects ─────────────────────────────────────────────────────────────────

@app.get("/projects/new", response_class=HTMLResponse)
def project_new_page(request: Request, user=Depends(require_role("admin"))):
    return templates.TemplateResponse(request, "project_form.html", {"user": user, "project": None})


@app.post("/projects/new")
def project_create(request: Request, user=Depends(require_role("admin")),
                   name: str = Form(...), address: str = Form(""), investor: str = Form(""),
                   contractor: str = Form(""), supervisor: str = Form(""),
                   start_date: str = Form(""), end_date: str = Form(""),
                   lat: float = Form(21.0), lon: float = Form(105.8)):
    c = connect()
    try:
        c.execute("""INSERT INTO projects (name, address, investor, contractor, supervisor, start_date, end_date, lat, lon)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (name, address, investor, contractor, supervisor, start_date, end_date, lat, lon))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/", status_code=302)


@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def project_edit_page(request: Request, project_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404)
        project = dict(row)
    finally:
        c.close()
    return templates.TemplateResponse(request, "project_form.html", {"user": user, "project": project})


@app.post("/projects/{project_id}/edit")
def project_update(request: Request, project_id: int, user=Depends(require_role("admin")),
                   name: str = Form(...), address: str = Form(""), investor: str = Form(""),
                   contractor: str = Form(""), supervisor: str = Form(""),
                   start_date: str = Form(""), end_date: str = Form(""),
                   lat: float = Form(21.0), lon: float = Form(105.8)):
    c = connect()
    try:
        c.execute("""UPDATE projects SET name=?, address=?, investor=?, contractor=?,
                     supervisor=?, start_date=?, end_date=?, lat=?, lon=? WHERE id=?""",
                  (name, address, investor, contractor, supervisor, start_date, end_date, lat, lon, project_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int, user: Annotated[dict, Depends(project_access_dep)]):
    c = connect()
    try:
        project = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(404)
        project = dict(project)
        diaries = [dict(r) for r in c.execute(
            "SELECT * FROM diaries WHERE project_id=? ORDER BY date DESC", (project_id,))]
        recipients = [dict(r) for r in c.execute(
            "SELECT * FROM recipients WHERE project_id=?", (project_id,))]
    finally:
        c.close()
    return templates.TemplateResponse(request, "project_detail.html", {
        "user": user, "project": project, "diaries": diaries, "recipients": recipients,
    })


# ── Diary form ───────────────────────────────────────────────────────────────

@app.get("/projects/{project_id}/diary/new", response_class=HTMLResponse)
async def diary_new(request: Request, project_id: int, user: Annotated[dict, Depends(project_write_dep)], date_str: str = None):
    c = connect()
    try:
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        project = dict(project_row)
        labor_types = [dict(r) for r in c.execute("SELECT * FROM labor_types WHERE COALESCE(is_active,1)=1 ORDER BY sort_order")]
        machinery_types = [dict(r) for r in c.execute("SELECT * FROM machinery_types WHERE COALESCE(is_active,1)=1 ORDER BY sort_order")]
        # Auto-fill from previous day
        target_date = date_str or date.today().isoformat()
        session = "morning" if datetime.now().hour < 12 else "afternoon"
        await ensure_weather(c, project_id, target_date, project.get("lat", 21.0), project.get("lon", 105.8), session)
        weather_rows = {r["session"]: dict(r) for r in c.execute(
            "SELECT session, temp, description AS desc, icon FROM project_weather_log "
            "WHERE project_id=? AND log_date=?", (project_id, target_date))}
        # Prefill the form server-side from the cached/just-fetched observation
        # so the weather is present even before (or without) the browser call.
        weather_default = {}
        for _session in ("morning", "afternoon"):
            _row = weather_rows.get(_session) or {}
            if _row.get("temp") is not None:
                weather_default = {"temp": _round_temp(_row.get("temp")),
                                   "desc": _row.get("desc", "")}
                break
        prev = c.execute(
            "SELECT * FROM diaries WHERE project_id=? AND date < ? ORDER BY date DESC LIMIT 1",
            (project_id, target_date)).fetchone()
        # Warn (and never overwrite) when the chosen date already has a diary.
        conflict = c.execute(
            "SELECT id FROM diaries WHERE project_id=? AND date=?", (project_id, target_date)).fetchone()
        date_conflict = conflict["id"] if conflict else None
        prev_labor = {}
        prev_machinery = {}
        prev_materials = []
        if prev:
            for r in c.execute("SELECT * FROM diary_labor WHERE diary_id=?", (prev["id"],)):
                prev_labor[r["labor_type_id"]] = r["count"]
            for r in c.execute("SELECT * FROM diary_machinery WHERE diary_id=?", (prev["id"],)):
                prev_machinery[r["machinery_type_id"]] = r["hours"]
            prev_materials = [dict(r) for r in c.execute(
                "SELECT * FROM diary_materials WHERE diary_id=?", (prev["id"],))]
            # Photos are NOT auto-filled from previous day
            prev_photos = []
        else:
            prev_photos = []
    finally:
        c.close()
    return templates.TemplateResponse(request, "diary_form.html", {
        "user": user, "project": project, "target_date": target_date,
        "labor_types": labor_types, "machinery_types": machinery_types,
        "prev_labor": prev_labor, "prev_machinery": prev_machinery,
        "prev_materials": prev_materials, "prev_photos": prev_photos,
        "diary": None,
        "date_conflict": date_conflict,
        "weather_default": weather_default,
        "weather_morning": weather_rows.get("morning", {}),
        "weather_afternoon": weather_rows.get("afternoon", {}),
    })


# ── Issue tracking helpers ──────────────────────────────────────────────────────

_ISSUE_LEVELS = ("none", "low", "medium", "high")
_WORKFLOW_STATUSES = ("pending", "resolved", "unresolved")


def _sanitize_text(value):
    """Strip raw HTML tags from user text so Telegram HTML parse_mode
    cannot be abused for injection."""
    import re
    if value is None:
        return ""
    s = str(value)
    s = re.sub(r"<[^>]*>", "", s)
    return s.strip()


def _html_escape(value):
    import html
    return html.escape(str(value) if value is not None else "", quote=False)


def _norm_issue_level(v):
    v = (v or "").strip().lower()
    return v if v in _ISSUE_LEVELS else "none"


def _norm_workflow(v):
    v = (v or "").strip().lower()
    return v if v in _WORKFLOW_STATUSES else "none"


def _norm_issue_row(row):
    """Normalise a raw diary_issues row into a plain dict with safe defaults."""
    if row is None:
        return None
    d = dict(row)
    d.setdefault("description", "")
    d.setdefault("cause", "")
    d.setdefault("related_party", "")
    d.setdefault("suggestion", "")
    d.setdefault("level", "none")
    d.setdefault("workflow_status", "pending")
    d.setdefault("resolved_at", None)
    d.setdefault("notified_at", None)
    return d


def _load_diary_issues(c, diary_id):
    """All child issues for a diary, ordered by id (oldest first)."""
    rows = c.execute(
        "SELECT * FROM diary_issues WHERE diary_id=? ORDER BY id",
        (diary_id,)).fetchall()
    return [_norm_issue_row(r) for r in rows]


def _unresolved_issue_rows(c, user):
    """All unresolved issues across the user's accessible projects.

    Reads the child table first; diaries that still only carry legacy
    single-issue columns synthesise one pseudo-issue via ``_legacy_issue_dict``.
    """
    if user["role"] == "admin":
        proj_filter = ""
        proj_params = []
    else:
        proj_filter = ("AND d.project_id IN (SELECT p.id FROM projects p "
                       "JOIN user_projects up ON p.id=up.project_id WHERE up.user_id=?)")
        proj_params = [user["id"]]
    rows = c.execute(
        """SELECT di.*, d.date, d.project_id, p.name AS project_name
           FROM diary_issues di
           JOIN diaries d ON d.id=di.diary_id
           JOIN projects p ON p.id=d.project_id
           WHERE di.workflow_status IN ('pending','unresolved')
             AND (di.description IS NOT NULL AND di.description != '')
             {pf}
           ORDER BY di.id DESC""".format(pf=proj_filter),
        proj_params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = d["id"]
        d["diary_id"] = d["diary_id"]
        out.append(d)
    # Legacy fallback: diaries with no child issues but a legacy single issue.
    legacy_rows = c.execute(
        """SELECT d.*, p.name AS project_name FROM diaries d
           JOIN projects p ON p.id=d.project_id
           WHERE (d.issues IS NOT NULL AND d.issues != '')
             AND d.workflow_status IN ('pending','unresolved')
             AND NOT EXISTS (SELECT 1 FROM diary_issues di WHERE di.diary_id=d.id)
             {pf}""".format(pf=proj_filter),
        proj_params).fetchall()
    for lr in legacy_rows:
        pseudo = _legacy_issue_dict(lr)
        if not pseudo:
            continue
        pseudo["date"] = lr["date"]
        pseudo["project_name"] = lr["name"]
        pseudo["project_id"] = lr["project_id"]
        pseudo["diary_id"] = lr["id"]
        out.append(pseudo)
    out.sort(key=lambda x: (x.get("id") or 0), reverse=True)
    return out


def _legacy_issue_dict(diary_row):
    """Synthesise a single pseudo-issue from legacy diary columns.

    Used only for read paths that still expect one issue per diary (the
    legacy ``diaries.issues`` column).  New code should read the child table.
    """
    if not diary_row:
        return None
    d = dict(diary_row)
    if not (d.get("issues") or "").strip():
        return None
    return {
        "id": None,
        "diary_id": d.get("id"),
        "description": d.get("issues") or "",
        "cause": d.get("cause") or "",
        "related_party": d.get("related_party") or "",
        "suggestion": d.get("issue_status") or "",
        "level": d.get("issue_level") or "none",
        "workflow_status": d.get("workflow_status") or "pending",
        "resolved_at": d.get("resolved_at"),
        "notified_at": d.get("issue_notified_at"),
    }


def _parse_issue_payload(form):
    """Parse dynamic multi-issue rows from a submitted form.

    Field naming per row index i (0-based):
      issue_count            hidden row count
      issue_{i}_desc         description (required for the row to count)
      issue_{i}_cause        cause
      issue_{i}_related      related party
      issue_{i}_suggestion   suggestion / issue_status
      issue_{i}_level        none|low|medium|high
      issue_{i}_workflow     pending|unresolved|resolved
      issue_{i}_resolved     resolve date
      issue_{i}_photo_ids    comma-separated existing photo ids for this issue
      issue_{i}_id            existing diary_issues.id (edit); preserved so the
                              durable ``notified_at`` marker survives edits.

    Backward compatible: when no ``issue_count`` is submitted the legacy
    single-issue fields are synthesised into one issue so old clients keep
    working.  Rows with an empty description are skipped.
    """
    raw = form.getlist("issue_count")
    count = 0
    if raw:
        try:
            count = int(raw[0])
        except (ValueError, TypeError):
            count = 0
    if not count:
        desc = form.get("issues", "")
        if desc and str(desc).strip():
            ws = _norm_workflow(form.get("workflow_status", "none"))
            resolved = form.get("resolved_at", "") or None
            ws, resolved = _resolve_date_logic(ws, resolved)
            return [{
                "id": None,
                "description": str(desc),
                "cause": _sanitize_text(form.get("cause", "")),
                "related_party": _sanitize_text(form.get("related_party", "")),
                "suggestion": form.get("issue_status", ""),
                "level": _norm_issue_level(form.get("issue_level", "none")),
                "workflow_status": ws,
                "resolved_at": resolved,
                "photo_ids": [],
            }]
        return []
    issues = []
    for i in range(count):
        desc = form.get(f"issue_{i}_desc", "") or ""
        if not str(desc).strip():
            continue
        ws = _norm_workflow(form.get(f"issue_{i}_workflow", "none"))
        resolved = form.get(f"issue_{i}_resolved", "") or None
        ws, resolved = _resolve_date_logic(ws, resolved)
        photo_ids = []
        for x in form.getlist(f"issue_{i}_photo_ids"):
            try:
                photo_ids.append(int(x))
            except (ValueError, TypeError):
                pass
        existing_id = None
        raw_id = form.get(f"issue_{i}_id", "")
        if raw_id:
            try:
                existing_id = int(raw_id)
            except (ValueError, TypeError):
                existing_id = None
        issues.append({
            "id": existing_id,
            "description": str(desc),
            "cause": _sanitize_text(form.get(f"issue_{i}_cause", "")),
            "related_party": _sanitize_text(form.get(f"issue_{i}_related", "")),
            "suggestion": form.get(f"issue_{i}_suggestion", ""),
            "level": _norm_issue_level(form.get(f"issue_{i}_level", "none")),
            "workflow_status": ws,
            "resolved_at": resolved,
            "photo_ids": photo_ids,
        })
    return issues


def _save_diary_issues(c, diary_id, issues_payload):
    """Replace child issues for a diary and link existing photos.

    Returns ``(created_rows, alert_triggers)`` where ``alert_triggers`` is a
    list of issue dicts whose level is ``high`` and has never been notified.

    Edit semantics: rows that carry an existing ``id`` are updated in place so
    the durable ``notified_at`` marker survives edits (no duplicate alert on
    ordinary edit).  Rows without an id are inserted.  Any child id that is no
    longer present in the payload is deleted.
    """
    c.execute("UPDATE diary_photos SET issue_id=NULL WHERE diary_id=?", (diary_id,))
    existing = {r["id"]: dict(r) for r in c.execute(
        "SELECT * FROM diary_issues WHERE diary_id=?", (diary_id,)).fetchall()}
    seen_ids = set()
    created = []
    alerts = []
    single_existing = [i for i in existing.values()] if len(existing) == 1 else []
    for payload in issues_payload:
        pid = payload.get("id")
        if pid and pid in existing:
            seen_ids.add(pid)
            prev = existing[pid]
            c.execute(
                """UPDATE diary_issues SET description=?, cause=?, related_party=?,
                       suggestion=?, level=?, workflow_status=?, resolved_at=?
                   WHERE id=? AND diary_id=?""",
                (payload["description"], payload["cause"], payload["related_party"],
                 payload["suggestion"], payload["level"], payload["workflow_status"],
                 payload["resolved_at"], pid, diary_id))
            row = dict(prev)
            row.update({
                "description": payload["description"], "cause": payload["cause"],
                "related_party": payload["related_party"], "suggestion": payload["suggestion"],
                "level": payload["level"], "workflow_status": payload["workflow_status"],
                "resolved_at": payload["resolved_at"],
            })
            row = _norm_issue_row(row)
        elif not pid and single_existing:
            # Legacy single-issue client: update the one existing child in place
            # so the durable notified_at marker survives re-saves.
            prev = single_existing[0]
            pid = prev["id"]
            seen_ids.add(pid)
            c.execute(
                """UPDATE diary_issues SET description=?, cause=?, related_party=?,
                       suggestion=?, level=?, workflow_status=?, resolved_at=?
                   WHERE id=? AND diary_id=?""",
                (payload["description"], payload["cause"], payload["related_party"],
                 payload["suggestion"], payload["level"], payload["workflow_status"],
                 payload["resolved_at"], pid, diary_id))
            row = dict(prev)
            row.update({
                "description": payload["description"], "cause": payload["cause"],
                "related_party": payload["related_party"], "suggestion": payload["suggestion"],
                "level": payload["level"], "workflow_status": payload["workflow_status"],
                "resolved_at": payload["resolved_at"],
            })
            row = _norm_issue_row(row)
        else:
            cur = c.execute(
                """INSERT INTO diary_issues (diary_id, description, cause, related_party,
                       suggestion, level, workflow_status, resolved_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (diary_id, payload["description"], payload["cause"],
                 payload["related_party"], payload["suggestion"], payload["level"],
                 payload["workflow_status"], payload["resolved_at"]))
            if IS_PG:
                pid = cur.fetchone()["id"]
            else:
                pid = cur.lastrowid
            seen_ids.add(pid)
            row = _norm_issue_row(
                c.execute("SELECT * FROM diary_issues WHERE id=?", (pid,)).fetchone())
        for ph_id in payload["photo_ids"]:
            c.execute("UPDATE diary_photos SET issue_id=? WHERE id=? AND diary_id=?",
                      (pid, ph_id, diary_id))
        created.append(row)
        if row["level"] == "high" and not row.get("notified_at"):
            alerts.append(row)
    for old_id in existing:
        if old_id not in seen_ids:
            c.execute("DELETE FROM diary_issues WHERE id=? AND diary_id=?",
                      (old_id, diary_id))
    return created, alerts


def _mark_issue_alerted(c, issue_id):
    """Durable per-issue notified marker, set before the Telegram call."""
    from datetime import datetime as _dt
    c.execute("UPDATE diary_issues SET notified_at=? WHERE id=?",
              (_dt.now().strftime("%Y-%m-%d %H:%M:%S"), issue_id))


async def _fire_issue_alert_for_issue(project_id, diary_id, issue_row):
    """Send a Telegram alert for one new high-severity issue.

    Called AFTER the DB commit so a Telegram failure can never roll back the
    diary save.  Returns a status string ("sent"/"failed"/"skipped").
    """
    from app.telegram_bot import send_message, get_bot_token, sanitize_error
    if not get_bot_token():
        return "skipped"
    c = connect()
    try:
        chat_ids = _issue_recipients(c, project_id)
        prow = c.execute("SELECT d.*, p.name AS project_name FROM diaries d "
                         "JOIN projects p ON p.id=d.project_id WHERE d.id=?",
                         (diary_id,)).fetchone()
    finally:
        c.close()
    if not chat_ids or not prow:
        return "skipped"
    prow = dict(prow)
    level = {"low": "🟢 Nhẹ", "medium": "🟡 Trung bình", "high": "🔴 Nghiêm trọng"}.get(
        issue_row.get("level", ""), "")
    msg_lines = [
        f"⚠️ <b>Có issue mới — dự án {prow.get('project_name', '')}</b>",
        f"📅 Ngày: {prow.get('date', '')}",
        f"🔴 Mức độ: {level}",
        f"📝 Mô tả: {_html_escape(issue_row.get('description', ''))[:500]}",
    ]
    if issue_row.get("cause"):
        msg_lines.append(f"🔍 Nguyên nhân: {_html_escape(issue_row.get('cause', ''))[:300]}")
    if issue_row.get("related_party"):
        msg_lines.append(f"👤 Liên quan: {_html_escape(issue_row.get('related_party', ''))[:200]}")
    if issue_row.get("suggestion"):
        msg_lines.append(f"💡 Kiến nghị: {_html_escape(issue_row.get('suggestion', ''))[:300]}")
    msg = "\n".join(msg_lines)
    if len(msg) > 4096:
        msg = msg[:4095] + "…"
    ok_count = 0
    fail_count = 0
    for chat_id in chat_ids:
        try:
            res = await send_message(chat_id, msg)
        except Exception as exc:
            res = {"ok": False, "error": sanitize_error(exc)}
        if res and res.get("ok"):
            ok_count += 1
        else:
            fail_count += 1
    return "sent" if ok_count else "failed"


def _resolve_date_logic(workflow_status, resolved_at):
    """Return (workflow_status, resolved_at) after applying resolve/reopen rules.

    Resolving with no explicit date stamps today; reopening clears the date.
    """
    from datetime import date as _date
    if workflow_status == "resolved":
        if not resolved_at:
            resolved_at = _date.today().isoformat()
    else:
        # pending / unresolved / none -> no resolution date.
        resolved_at = None
    return workflow_status, resolved_at


def _issue_recipients(c, project_id):
    """Chat ids for a project's configured Telegram recipients + assigned users."""
    chat_ids = set()
    for r in c.execute(
            "SELECT telegram_chat_id FROM recipients WHERE project_id=? "
            "AND telegram_chat_id IS NOT NULL AND telegram_chat_id != ''",
            (project_id,)):
        if r["telegram_chat_id"]:
            chat_ids.add(r["telegram_chat_id"])
    for r in c.execute(
            """SELECT u.telegram_chat_id FROM users u
               JOIN user_projects up ON u.id=up.user_id
               WHERE up.project_id=? AND u.telegram_chat_id IS NOT NULL
               AND u.telegram_chat_id != ''""", (project_id,)):
        if r["telegram_chat_id"]:
            chat_ids.add(r["telegram_chat_id"])
    return chat_ids


def _should_alert(diary_row, new_level, new_notified_at):
    """Decide whether a high-severity alert must fire now.

    Fires when the diary becomes high-severity and has not been notified for
    high yet.  Escalation from non-high to high alerts exactly once; ordinary
    edits of an already-notified high issue do not re-alert.
    """
    if new_level != "high":
        return False
    if new_notified_at:
        return False
    return True


async def _fire_issue_alert(project_id, diary_id, diary_row):
    """Send a concise Telegram alert for a new high-severity issue.

    Called AFTER the DB commit so a Telegram failure can never roll back the
    diary save.  Returns a status string ("sent"/"failed"/"skipped").
    """
    from app.telegram_bot import send_message, get_bot_token, sanitize_error
    if not get_bot_token():
        return "skipped"
    c = connect()
    try:
        chat_ids = _issue_recipients(c, project_id)
    finally:
        c.close()
    if not chat_ids:
        return "skipped"
    level = {"low": "🟢 Nhẹ", "medium": "🟡 Trung bình", "high": "🔴 Nghiêm trọng"}.get(
        diary_row.get("issue_level", ""), "")
    msg_lines = [
        f"⚠️ <b>Có issue mới — dự án {diary_row.get('project_name', '')}</b>",
        f"📅 Ngày: {diary_row.get('date', '')}",
        f"🔴 Mức độ: {level}",
        f"📝 Mô tả: {_html_escape(diary_row.get('issues', ''))[:500]}",
    ]
    if diary_row.get("cause"):
        msg_lines.append(f"🔍 Nguyên nhân: {_html_escape(diary_row.get('cause', ''))[:300]}")
    if diary_row.get("related_party"):
        msg_lines.append(f"👤 Liên quan: {_html_escape(diary_row.get('related_party', ''))[:200]}")
    if diary_row.get("issue_status"):
        msg_lines.append(f"💡 Kiến nghị: {_html_escape(diary_row.get('issue_status', ''))[:300]}")
    msg = "\n".join(msg_lines)
    if len(msg) > 4096:
        msg = msg[:4095] + "…"
    ok_count = 0
    fail_count = 0
    for chat_id in chat_ids:
        try:
            res = await send_message(chat_id, msg)
        except Exception as exc:
            res = {"ok": False, "error": sanitize_error(exc)}
        if res and res.get("ok"):
            ok_count += 1
        else:
            fail_count += 1
    return "sent" if ok_count else "failed"


def _add_timemark(image_path, text):
    """Add timestamp watermark to image using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)
        # Try to load a font
        font = None
        for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/DejaVuSans.ttf"]:
            if __import__("os").path.exists(fp):
                font = ImageFont.truetype(fp, 24)
                break
        if font is None:
            font = ImageFont.load_default()
        # Position: bottom-right
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = img.width - tw - 20
        y = img.height - th - 20
        # Draw background
        draw.rectangle([x - 5, y - 5, x + tw + 5, y + th + 5], fill=(0, 0, 0, 128))
        draw.text((x, y), text, fill=(255, 255, 255), font=font)
        img.save(image_path)
    except Exception as e:
        print("Timemark error:", e)


async def _persist_photo(file_obj, diary_id: int, stamp: str):
    """Read an uploaded file, validate/normalize it, reserve quota, store it.

    Returns ``(filename, byte_count)`` on success or ``(None, error_msg)``
    when the upload is empty, malformed, over-limit or the storage write
    fails.  The quota ledger is released on any failure so nothing is
    silently lost.
    """
    content = await file_obj.read()
    if not content:
        return None, None
    normalized, err = r2.validate_image(content)
    if err:
        return None, err
    if not normalized:
        return None, "Ảnh rỗng sau khi xử lý"
    ext = Path(file_obj.filename).suffix.lower() or ".jpg"
    fname = f"diary_{diary_id}_{uuid.uuid4().hex}{ext}"
    # Photo-count ceiling check (before touching storage).  The count is the
    # only limit we can enforce before the timemark changes the byte size.
    if r2.enabled():
        c = connect()
        try:
            existing = c.execute(
                "SELECT COUNT(*) AS n FROM diary_photos WHERE diary_id=?",
                (diary_id,)).fetchone()["n"]
        finally:
            c.close()
        photo_count = existing + 1
    else:
        photo_count = 1
    tmp = UPLOAD_DIR / f".tmp_{fname}"
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(normalized)
        try:
            _add_timemark(str(tmp), stamp)
            normalized = tmp.read_bytes()
        except Exception as te:
            print("Timemark error:", te)
        # Reserve against the FINAL stored size: the timemark can change the
        # byte count, so the ledger must account for what actually lands on
        # storage, not the raw upload.
        final_size = len(normalized)
        ok, msg = r2.reserve(final_size, photo_count)
        if not ok:
            return None, msg
        r2.save_file(fname, normalized)
    except Exception as exc:
        r2.release(len(normalized))
        try:
            tmp.unlink()
        except OSError:
            pass
        return None, "Lỗi lưu ảnh: %s" % exc
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return fname, final_size


async def _queue_photo_replacements(c, diary_id, existing_ids, existing_captions, form, prior_ids=None):
    """Apply existing-photo changes (delete / replace / caption) safely.

    Replacements are written to storage first and only committed to the DB
    (replacing the old filename) *before* the transaction commits; the old
    object is removed only after a successful commit. Returns a list of old
    ``(filename, reserved_bytes)`` tuples deferred for cleanup.

    Only photos that already existed *before* this request are touched:
    ``prior_ids`` is a snapshot of the diary's photo ids taken before the
    route inserts any new uploads, so a photo just uploaded in the same
    request is never deleted. When ``prior_ids`` is omitted (legacy call
    sites) every current row is eligible for deletion.

    The replacement file is looked up per photo id (form field
    ``existing_photo_replace_<id>``) instead of by list index: the template
    renders a gallery *and* a camera picker per row, both sharing the same
    per-id field name, so a flat ``getlist("existing_photo_replace")``
    returns twice as many entries as there are photos and an index-based
    pairing silently replaces the wrong photo once more than one existing
    photo is present.
    """
    from datetime import datetime
    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    kept_ids = {int(pid) for pid in existing_ids if pid}
    prior_set = set(prior_ids) if prior_ids is not None else None
    deferred_old = []
    rows = c.execute("SELECT id, filename, size_bytes FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()
    for row in rows:
        pid = int(row["id"])
        # Never delete a photo that was just inserted in this request.
        if prior_set is not None and pid not in prior_set:
            continue
        if pid not in kept_ids:
            c.execute("DELETE FROM diary_photos WHERE id=? AND diary_id=?", (pid, diary_id))
            deferred_old.append((row["filename"], int(row["size_bytes"] or 0)))
    for i, pid in enumerate(existing_ids):
        if not pid:
            continue
        pid = int(pid)
        row = c.execute("SELECT filename, diary_id, caption, size_bytes FROM diary_photos WHERE id=? AND diary_id=?", (pid, diary_id)).fetchone()
        if not row:
            continue
        # Pick the first non-empty uploaded file for this photo id (gallery
        # and camera share the same per-id field name).
        files = form.getlist(f"existing_photo_replace_{pid}")
        f = next((x for x in files if x and getattr(x, "filename", "")), None)
        submitted_caption = existing_captions[i] if i < len(existing_captions) else ""
        if f:
            fname, reserved = await _persist_photo(f, row["diary_id"], stamp)
            if fname:
                cap = submitted_caption if submitted_caption.strip() else (row["caption"] or "")
                c.execute("UPDATE diary_photos SET filename=?, caption=?, size_bytes=? WHERE id=? AND diary_id=?",
                          (fname, cap, reserved, pid, diary_id))
                deferred_old.append((row["filename"], int(row["size_bytes"] or 0)))
                continue
        cap = submitted_caption if submitted_caption.strip() else (row["caption"] or "")
        c.execute("UPDATE diary_photos SET caption=? WHERE id=? AND diary_id=?", (cap, pid, diary_id))
    return deferred_old


def _cleanup_replaced_files(deferred_old):
    """Remove old objects only after the new ones are safely on storage.

    ``deferred_old`` is a list of ``(filename, reserved_bytes)`` tuples.  The
    old reservation is released *only* after ``delete_file`` confirms the old
    object is gone, so a storage failure never frees quota that is still in use.
    """
    for old_filename, reserved in deferred_old:
        if not old_filename:
            continue
        if r2.delete_file(old_filename):
            r2.release(reserved)


def _parse_id_list(values):
    """Parse id values that may arrive as separate fields or one comma-joined string.

    The diary form collapses its photo checkboxes into a single
    ``"12,13"`` field, so callers cannot assume one id per value; anything that
    is not a positive integer is ignored instead of raising a 500.
    """
    ids = set()
    for raw in values or []:
        for token in str(raw or "").replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                ids.add(int(token))
            except (TypeError, ValueError):
                continue
    return ids


def _valid_diary_date(value):
    """Return the canonical ISO date string when valid, otherwise None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def _round_temp(value):
    """Weather temperature is a whole number of degrees.

    OpenWeather returns values like 31.44; storing/displaying them as-is both
    breaks the number input's step validation and is meaningless for a site
    diary. Half-up rounding keeps 31.5 -> 32 (intuitive for operators).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _form_truthy(value):
    return str(value or "").strip().lower() in ("1", "on", "true", "yes")


# Templates render temperatures through this filter so every screen shows the
# same whole degree value the server stores.
templates.env.filters["tempdeg"] = _round_temp


def _issue_photo_flags(form):
    """Return ``(existing_issue_photo_ids, new_photos_are_issue)``.

    New uploads are marked as issue photos when the form sends the explicit
    ``new_issue_photo`` flag, or when a legacy client sends the sentinel photo
    id ``0`` (the old "this upload is an issue photo" convention). The sentinel
    is never a real photo id, so it is dropped from the id list.
    """
    ids = _parse_id_list(form.getlist("issue_photo_ids"))
    mark_new = _form_truthy(form.get("new_issue_photo"))
    if 0 in ids:
        mark_new = True
        ids.discard(0)
    return ids, mark_new


def _next_day_iso(iso_date: str) -> str:
    try:
        return (datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=1)).date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


def _diary_date_conflict_response(project_id: int, diary_date: str, diary_id: int):
    """409 page shown instead of overwriting a diary that already exists.

    Offers the only two safe continuations: edit the saved diary, or create a
    new one for another date.
    """
    next_date = _next_day_iso(diary_date)
    return HTMLResponse(
        "<h3>Ngày {d} đã có nhật ký</h3>"
        "<p>Hệ thống <strong>không ghi đè</strong> nhật ký đã có. "
        "Bố vui lòng chọn ngày khác hoặc mở nhật ký của ngày đó để sửa.</p>"
        "<p><a class=\"btn sm primary\" href=\"/projects/{pid}/diary/{did}/edit\">"
        "✏️ Mở nhật ký ngày {d} để sửa</a></p>"
        "<p><a class=\"btn sm\" href=\"/projects/{pid}/diary/new?date_str={nxt}\">"
        "🆕 Tạo nhật ký cho ngày {nxt}</a> "
        "<a class=\"btn sm\" href=\"/projects/{pid}\">← Về dự án</a></p>".format(
            d=diary_date, pid=project_id, did=diary_id, nxt=next_date),
        status_code=409)


def _is_upload(value):
    return value is not None and bool(getattr(value, "filename", ""))


def _getlist_any(form, names):
    for name in names:
        try:
            items = form.getlist(name)
        except Exception:
            items = []
        if items:
            return items
    return []


def _weather_form_value(form, prefix, fallback=None):
    """Read a weather temperature field, rounded to whole degrees."""
    raw = form.get(prefix)
    if raw in (None, ""):
        return fallback
    return _round_temp(raw)


_GALLERY_ALIASES = ("photos_gallery", "photo_gallery", "gallery_photos")
_CAMERA_ALIASES = ("photos_camera", "photo_camera", "camera_photos")
_CAPTION_ALIASES = ("photo_captions", "photo_caption", "captions", "photo_descriptions")
_NEW_PHOTO_ROW_RE = re.compile(r"^photo(?:_file)?_(\d+)$")


def _extract_new_photos(form):
    """Pair newly-uploaded files with their captions robustly.

    Rows in the form render two file pickers (gallery + camera) and one
    caption.  Empty/unused pickers must not shift caption indices, so we
    prefer per-picker (or per-row) fields and align by row index.  Legacy
    ``photos`` submissions are still handled for backward compatibility.
    Returns a list of ``(upload_file, caption_str)`` for selected files only.
    """
    # Row-indexed fields: photo_0 / photo_caption_0 etc.
    row_indices = set()
    for key in list(form.keys()):
        m = _NEW_PHOTO_ROW_RE.match(str(key))
        if m:
            row_indices.add(int(m.group(1)))
    # also caption-indexed
    for key in list(form.keys()):
        m = __import__("re").match(r"^photo_caption_(\d+)$", str(key))
        if m:
            row_indices.add(int(m.group(1)))
    if row_indices:
        out = []
        for idx in sorted(row_indices):
            f = None
            for name in (f"photo_{idx}", f"photo_file_{idx}", f"photo_{idx}_file"):
                vals = form.getlist(name) if hasattr(form, "getlist") else []
                if vals:
                    f = vals[0]
                    break
            cap_vals = []
            for name in (f"photo_caption_{idx}", f"caption_{idx}"):
                try:
                    cap_vals = form.getlist(name)
                except Exception:
                    cap_vals = []
                if cap_vals:
                    break
            cap = cap_vals[0] if cap_vals else ""
            if _is_upload(f):
                out.append((f, cap if cap is not None else ""))
            # gallery/camera pair for same row index
            for gname in (f"photo_gallery_{idx}", f"gallery_{idx}"):
                for gf in (form.getlist(gname) if hasattr(form, "getlist") else []):
                    if _is_upload(gf):
                        out.append((gf, cap if cap is not None else ""))
            for cname in (f"photo_camera_{idx}", f"camera_{idx}"):
                for cf in (form.getlist(cname) if hasattr(form, "getlist") else []):
                    if _is_upload(cf):
                        out.append((cf, cap if cap is not None else ""))
        if out:
            return out

    galleries = _getlist_any(form, _GALLERY_ALIASES)
    cameras = _getlist_any(form, _CAMERA_ALIASES)
    captions = _getlist_any(form, _CAPTION_ALIASES)
    if galleries or cameras:
        n = max(len(galleries), len(cameras), len(captions))
        out = []
        for i in range(n):
            g = galleries[i] if i < len(galleries) else None
            c = cameras[i] if i < len(cameras) else None
            cap = captions[i] if i < len(captions) else ""
            if cap is None:
                cap = ""
            for f in (g, c):
                if _is_upload(f):
                    out.append((f, cap))
        return out

    files = form.getlist("photos") if hasattr(form, "getlist") else []
    captions = _getlist_any(form, _CAPTION_ALIASES)
    if not captions:
        return [(f, "") for f in files if _is_upload(f)]
    # Aligned 1:1
    if len(files) == len(captions):
        out = []
        for i, f in enumerate(files):
            if _is_upload(f):
                cap = captions[i] if i < len(captions) else ""
                out.append((f, cap if cap is not None else ""))
        return out
    # Two pickers per row (gallery + camera) -> chunk into pairs.
    if len(captions) > 0 and len(files) == 2 * len(captions):
        out = []
        for r in range(len(captions)):
            cap = captions[r] if captions[r] is not None else ""
            for j in (0, 1):
                idx = 2 * r + j
                f = files[idx] if idx < len(files) else None
                if _is_upload(f):
                    out.append((f, cap))
        return out
    # Fallback legacy pairing.
    out = []
    for i, f in enumerate(files):
        if _is_upload(f):
            cap = captions[i] if i < len(captions) else ""
            out.append((f, cap if cap is not None else ""))
    return out


@app.post("/projects/{project_id}/diary/save")
async def diary_save(request: Request, project_id: int, user: Annotated[dict, Depends(project_write_dep)]):
    import traceback
    try:
        form = await request.form()
    except Exception as e:
        print("Form parse error:", e, traceback.format_exc())
        raise
    diary_date = _valid_diary_date(form.get("date"))
    if not diary_date:
        return HTMLResponse(
            "<h3>Ngày thi công không hợp lệ</h3>"
            "<p>Chọn ngày theo định dạng YYYY-MM-DD rồi lưu lại.</p>",
            status_code=400)
    weather_temp = _round_temp(form.get("weather_temp"))
    weather_desc = form.get("weather_desc")
    morning_temp = _weather_form_value(form, "weather_morning_temp", weather_temp)
    morning_desc = form.get("weather_morning_desc") or weather_desc
    afternoon_temp = _weather_form_value(form, "weather_afternoon_temp")
    afternoon_desc = form.get("weather_afternoon_desc") or ""
    foreman = form.get("foreman", "")
    notes = form.get("notes", "")
    plan_tomorrow = form.get("plan_tomorrow", "")
    issues = form.get("issues", "")
    issue_level = _norm_issue_level(form.get("issue_level", "none"))
    issue_status = form.get("issue_status", "")
    cause = _sanitize_text(form.get("cause", ""))
    related_party = _sanitize_text(form.get("related_party", ""))
    workflow_status = _norm_workflow(form.get("workflow_status", "none"))
    resolved_at = form.get("resolved_at", "") or None
    workflow_status, resolved_at = _resolve_date_logic(workflow_status, resolved_at)
    # Dynamic multi-issue payload (new path).  Falls back to the legacy
    # single-issue fields when the form does not submit issue_count.
    issues_payload = _parse_issue_payload(form)
    # Auto-calculate total workers from all labor_ fields
    total_workers = 0
    for key, val in form.items():
        if key.startswith("labor_") and key != "labor_ids":
            try:
                total_workers += int(val) if val and val.strip() else 0
            except ValueError:
                pass

    c = connect()
    try:
        # Never overwrite an existing diary: one diary per project+date. The
        # operator is sent to the saved diary (or picks another date) instead.
        existing = c.execute(
            "SELECT id FROM diaries WHERE project_id=? AND date=?", (project_id, diary_date)).fetchone()
        if existing:
            return _diary_date_conflict_response(project_id, diary_date, existing["id"])
        store_weather(c, project_id, diary_date, "morning", {"temp": morning_temp, "desc": morning_desc})
        store_weather(c, project_id, diary_date, "afternoon", {"temp": afternoon_temp, "desc": afternoon_desc})
        cur = c.execute("""INSERT INTO diaries (project_id, date, weather_temp, weather_desc,
                         foreman, total_workers, notes, plan_tomorrow, issues, issue_level, issue_status,
                         cause, related_party, workflow_status, resolved_at, created_by)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""" if __import__("app.db", fromlist=["IS_PG"]).IS_PG else """INSERT INTO diaries (project_id, date, weather_temp, weather_desc,
                         foreman, total_workers, notes, plan_tomorrow, issues, issue_level, issue_status,
                         cause, related_party, workflow_status, resolved_at, created_by)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (project_id, diary_date, morning_temp, morning_desc, foreman,
                         total_workers, notes, plan_tomorrow, issues, issue_level, issue_status, cause,
                         related_party, workflow_status, resolved_at, user["id"]))
        if __import__("app.db", fromlist=["IS_PG"]).IS_PG:
            diary_id = cur.fetchone()["id"]
        else:
            diary_id = cur.lastrowid

        # Labor (dropdown approach)
        labor_ids_str = form.get("labor_ids", "")
        for lt_id in _parse_id_list([labor_ids_str]):
            try:
                count = int(form.get(f"labor_{lt_id}", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
                          (diary_id, lt_id, count))
        # Machinery (dropdown approach)
        machinery_ids_str = form.get("machinery_ids", "")
        for mt_id in _parse_id_list([machinery_ids_str]):
            qty_str = form.get(f"machinery_{mt_id}", "0")
            try:
                qty = int(qty_str) if qty_str and qty_str.strip() else 0
            except (ValueError, TypeError):
                qty = 0
            if qty > 0:
                c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
                          (diary_id, mt_id, qty))
        # Materials
        mat_names = form.getlist("mat_name")
        mat_units = form.getlist("mat_unit")
        mat_qtys = form.getlist("mat_qty")
        for i, name in enumerate(mat_names):
            if name.strip():
                c.execute("INSERT INTO diary_materials (diary_id, name, unit, quantity) VALUES (?,?,?,?)",
                          (diary_id, name, mat_units[i] if i < len(mat_units) else "",
                           float(mat_qtys[i]) if i < len(mat_qtys) and mat_qtys[i] and mat_qtys[i].strip() else 0))
        # Tasks
        task_names = form.getlist("task_name")
        task_qtys = form.getlist("task_qty")
        task_units = form.getlist("task_unit")
        task_pcts = form.getlist("task_pct")
        for i, name in enumerate(task_names):
            if name.strip():
                c.execute("INSERT INTO diary_tasks (diary_id, task_name, quantity, unit, cumulative_pct) VALUES (?,?,?,?,?)",
                          (diary_id, name,
                           float(task_qtys[i]) if i < len(task_qtys) and task_qtys[i] and task_qtys[i].strip() else 0,
                           task_units[i] if i < len(task_units) else "",
                           float(task_pcts[i]) if i < len(task_pcts) and task_pcts[i] and task_pcts[i].strip() else 0))

        # Acceptance (nghiệm thu)
        acc_item_names = form.getlist("acceptance_0_item_name")
        # Check if we have acceptance data (multiple rows with indices)
        # We need to parse by index
        acc_indices = set()
        for key in form.keys():
            if key.startswith("acceptance_") and "_item_name" in key:
                idx = key.split("_")[1]
                if idx.isdigit():
                    acc_indices.add(int(idx))
        
        for idx in sorted(acc_indices):
            item_name = form.get(f"acceptance_{idx}_item_name", "")
            if not item_name.strip():
                continue
            spec = form.get(f"acceptance_{idx}_spec", "")
            quantity = form.get(f"acceptance_{idx}_quantity", "")
            unit = form.get(f"acceptance_{idx}_unit", "")
            result = form.get(f"acceptance_{idx}_result", "passed")
            inspector = form.get(f"acceptance_{idx}_inspector", "")
            note = form.get(f"acceptance_{idx}_note", "")
            
            try:
                qty = float(quantity) if quantity and quantity.strip() else 0
            except (ValueError, TypeError):
                qty = 0
            
            c.execute("""INSERT INTO diary_acceptance (diary_id, item_name, spec, quantity, unit, result, note, inspector)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (diary_id, item_name, spec, qty, unit, result, note, inspector))

        # Photos with captions
        prior_ids = [r["id"] for r in c.execute(
            "SELECT id FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()]
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%d/%m/%Y %H:%M")
        issue_photo_ids, new_photos_are_issue = _issue_photo_flags(form)
        photo_errors = []
        for f, caption in _extract_new_photos(form):
            fname, reserved = await _persist_photo(f, diary_id, stamp)
            if not fname:
                photo_errors.append(reserved or "Lỗi upload ảnh")
                continue
            kind = "issue" if new_photos_are_issue else "work"
            c.execute("INSERT INTO diary_photos (diary_id, filename, caption, size_bytes, photo_kind) VALUES (?,?,?,?,?)",
                      (diary_id, fname, caption if caption is not None else "", reserved, kind))
        if photo_errors:
            c.rollback()
            c.close()
            return HTMLResponse(
                "<h3>Lỗi upload ảnh</h3><ul>%s</ul>"
                % "".join("<li>%s</li>" % e for e in photo_errors),
                status_code=400)

        # Update/delete/replace existing photos
        existing_ids = form.getlist("existing_photo_ids")
        existing_captions = form.getlist("existing_photo_captions")
        if "existing_photo_mode" in form:
            deferred_old = await _queue_photo_replacements(
                        c, diary_id, existing_ids, existing_captions, form, prior_ids)
        else:
            deferred_old = []

        # Mark issue photos explicitly when the form says so.
        for pid in issue_photo_ids:
            c.execute("UPDATE diary_photos SET photo_kind='issue' WHERE id=? AND diary_id=?",
                      (pid, diary_id))

        # New one-to-many write path: replace child issues and link photos.
        created_issues, alert_triggers = _save_diary_issues(c, diary_id, issues_payload)
        for issue in alert_triggers:
            _mark_issue_alerted(c, issue["id"])

        # Legacy single-issue marker kept for backward compatibility with old
        # readers that still consult diaries.issue_notified_at.
        if issues_payload and any(i["level"] == "high" for i in issues_payload):
            from datetime import datetime as _dt2
            c.execute("UPDATE diaries SET issue_level='high', issue_notified_at=? WHERE id=?",
                      (_dt2.now().strftime("%Y-%m-%d %H:%M:%S"), diary_id))
        elif issues_payload:
            top = issues_payload[0]
            c.execute("UPDATE diaries SET issue_level=?, issue_status=?, cause=?, "
                      "related_party=?, workflow_status=?, resolved_at=? WHERE id=?",
                      (top["level"], top["suggestion"], top["cause"],
                       top["related_party"], top["workflow_status"], top["resolved_at"],
                       diary_id))

        c.commit()
        _cleanup_replaced_files(deferred_old)

        # Telegram alert runs AFTER commit; failure must not roll back the diary.
        alert_status = "skipped"
        for issue in alert_triggers:
            status = await _fire_issue_alert_for_issue(project_id, diary_id, issue)
            if status != "skipped":
                alert_status = status
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── Weather API proxy ────────────────────────────────────────────────────────

# ── Admin: Settings ───────────────────────────────────────────────────────────

@app.post("/api/test-telegram")
async def test_telegram_send(chat_id: str = Form(""), user=Depends(require_role("admin"))):
    """Admin-only: send a Telegram test message to a specific chat_id."""
    from app.telegram_bot import send_message, get_bot_token, sanitize_error

    token = get_bot_token()
    if not token:
        return {"ok": False, "error": "Chưa cấu hình Bot Token"}

    if not chat_id or not chat_id.strip():
        return {"ok": False, "error": "Chưa nhập Chat ID"}

    try:
        result = await send_message(
            chat_id.strip(), "🧪 Test từ Nhật ký thi công - Cấu hình Telegram hoạt động!")
    except Exception as e:
        return {"ok": False, "error": sanitize_error(e)}
    if isinstance(result, dict) and result.get("ok"):
        return {"ok": True}
    error = result.get("error") if isinstance(result, dict) else None
    return {"ok": False, "error": error or "Gửi tin nhắn test thất bại"}


@app.get("/api/test-telegram")
async def test_telegram(user=Depends(require_role("admin"))):
    """Admin-only: verify the configured bot token via getMe."""
    import httpx
    # Read token from settings
    c = connect()
    try:
        row = c.execute("SELECT value FROM settings WHERE key='telegram_bot_token'").fetchone()
    finally:
        c.close()
    token = row["value"] if row and row["value"] else ""
    if not token:
        token = __import__("os").getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "Chưa cấu hình Bot Token"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = r.json()
        if data.get("ok"):
            return {"ok": True, "bot": data["result"]["username"]}
        return {"ok": False, "error": data.get("description", "Token sai")}
    except Exception as e:
        from app.telegram_bot import sanitize_error
        return {"ok": False, "error": sanitize_error(e)}


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, user=Depends(require_role("admin"))):
    c = connect()
    try:
        settings = {}
        for r in c.execute("SELECT key, value FROM settings"):
            settings[r["key"]] = r["value"]
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_settings.html", {
        "user": user, "settings": settings,
    })


@app.post("/admin/settings")
def admin_settings_save(request: Request, user=Depends(require_role("admin")),
                       openweather_api_key: str = Form(""),
                       telegram_bot_token: str = Form("")):
    c = connect()
    try:
        for key, val in [("openweather_api_key", openweather_api_key),
                         ("telegram_bot_token", telegram_bot_token)]:
            try:
                c.execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, val.strip()))
            except Exception:
                c.execute("UPDATE settings SET value=? WHERE key=?", (val.strip(), key))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/settings", status_code=302)


# ── Admin: Storage quota ──────────────────────────────────────────────────────

@app.get("/admin/storage", response_class=HTMLResponse)
def admin_storage(request: Request, user=Depends(require_role("admin"))):
    status = r2.quota_status()
    used = r2.get_used_bytes()
    cap = status["cap"]
    pct = (used / cap * 100.0) if cap else 0.0
    low = pct >= 80.0
    exact = True
    if r2.enabled():
        try:
            _, exact = r2.reconcile_from_inventory()
        except Exception:
            exact = False
    else:
        # No R2: the ledger is the only source of truth; treat as exact only
        # when every row has a recorded size_bytes.
        c = connect()
        try:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM diary_photos WHERE size_bytes IS NULL"
            ).fetchone()
            exact = not (row and row["n"])
        finally:
            c.close()
    return templates.TemplateResponse(request, "admin_storage.html", {
        "user": user, "status": status, "used": used, "cap": cap,
        "pct": pct, "low": low, "exact": exact,
    })


@app.post("/admin/storage/reconcile")
def admin_storage_reconcile(request: Request, user=Depends(require_role("admin"))):
    if not r2.enabled():
        return RedirectResponse(url="/admin/storage", status_code=302)
    try:
        r2.reconcile_from_inventory()
    except Exception:
        pass
    return RedirectResponse(url="/admin/storage", status_code=302)


# ── Admin: Reminder scheduler ─────────────────────────────────────────────────

@app.get("/admin/reminders", response_class=HTMLResponse)
def admin_reminders(request: Request, user=Depends(require_role("admin"))):
    c = connect()
    try:
        projects = [dict(r) for r in c.execute(
            "SELECT * FROM projects ORDER BY id DESC").fetchall()]
        for p in projects:
            row = c.execute(
                "SELECT enabled, time FROM project_reminders WHERE project_id=?",
                (p["id"],)).fetchone()
            if row:
                p["reminder_enabled"] = row["enabled"]
                p["reminder_time"] = row["time"]
            else:
                p["reminder_enabled"] = p.get("reminder_enabled", 1)
                p["reminder_time"] = p.get("reminder_time", "08:00")
        state = dict(_REMINDER_STATE)
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_reminders.html", {
        "user": user, "projects": projects, "state": state,
    })


@app.post("/admin/reminders/{project_id}")
def admin_reminders_save(request: Request, project_id: int,
                         user=Depends(require_role("admin")),
                         reminder_enabled: str = Form("1"),
                         reminder_time: str = Form("08:00")):
    c = connect()
    try:
        enabled = 1 if reminder_enabled in ("1", "on", "true", "True") else 0
        project_row = c.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        c.execute(
            "INSERT INTO project_reminders (project_id, enabled, time) VALUES (?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled, "
            "time=excluded.time",
            (project_id, enabled, reminder_time))
        c.execute(
            "UPDATE projects SET reminder_enabled=?, reminder_time=? WHERE id=?",
            (enabled, reminder_time, project_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/reminders", status_code=302)


@app.get("/api/reminders/run")
async def api_reminders_run(project_id: int = None, force: bool = False,
                          user=Depends(require_role("admin"))):
    """Manual trigger: run due reminders now (or for one project). Admin only."""
    from app.telegram_bot import send_reminder, _reminder_tick
    if project_id:
        result = await send_reminder(int(project_id), force=force)
    else:
        result = await _reminder_tick(force=force)
    return result


@app.get("/api/reminders/status")
def api_reminders_status(user=Depends(require_role("admin"))):
    """Admin-only scheduler state."""
    return dict(_REMINDER_STATE)


@app.post("/projects/{project_id}/reminders")
def project_reminders_save(request: Request, project_id: int,
                          user=Depends(require_role("admin")),
                          reminder_enabled: str = Form("1"),
                          reminder_time: str = Form("08:00")):
    """Save reminder settings for a single project (used by the project page)."""
    c = connect()
    try:
        enabled = 1 if reminder_enabled in ("1", "on", "true", "True") else 0
        project_row = c.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        c.execute(
            "INSERT INTO project_reminders (project_id, enabled, time) VALUES (?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled, "
            "time=excluded.time",
            (project_id, enabled, reminder_time))
        c.execute(
            "UPDATE projects SET reminder_enabled=?, reminder_time=? WHERE id=?",
            (enabled, reminder_time, project_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


@app.get("/api/weather")
async def weather_proxy(project_id: int = None, lat: float = None, lon: float = None,
                        date: str = None):
    """Weather payload for the diary form.

    The diary form asks by ``project_id`` so the answer is always keyed to the
    project's own location and cached observation. Coordinates are only a
    fallback for callers that have no project (the browser's geolocation never
    matches a stored project position exactly).
    """
    import httpx
    from datetime import date as d
    target = date or d.today().isoformat()
    today = d.today().isoformat()
    api_key = None
    cached = {}
    c = connect()
    try:
        if project_id:
            project = c.execute("SELECT lat, lon FROM projects WHERE id=?", (project_id,)).fetchone()
            if project:
                lat = project["lat"] if project["lat"] is not None else 21.0
                lon = project["lon"] if project["lon"] is not None else 105.8
            rows = [dict(r) for r in c.execute(
                "SELECT session, temp, description AS desc, icon FROM project_weather_log "
                "WHERE project_id=? AND log_date=?", (project_id, target))]
            # Prefer a session that actually holds an observation.
            cached = next((r for r in rows if r.get("temp") is not None), {})
        elif lat is not None and lon is not None:
            project = c.execute(
                "SELECT id FROM projects WHERE lat=? AND lon=? LIMIT 1", (lat, lon)).fetchone()
            if project:
                rows = [dict(r) for r in c.execute(
                    "SELECT session, temp, description AS desc, icon FROM project_weather_log "
                    "WHERE project_id=? AND log_date=?", (project["id"], target))]
                cached = next((r for r in rows if r.get("temp") is not None), {})
        setting = c.execute("SELECT value FROM settings WHERE key='openweather_api_key'").fetchone()
        if setting and setting["value"]:
            api_key = setting["value"]
    finally:
        c.close()

    if cached:
        return {"temp": cached["temp"], "desc": cached.get("desc", ""),
                "icon": cached.get("icon", ""), "source": "cache"}

    # The free OpenWeather endpoint only serves the current day.
    if target != today:
        return {"temp": None, "desc": "Chưa có dữ liệu thời tiết lưu cho ngày này", "icon": "",
                "source": "none"}

    api_key = api_key or os.getenv("OPENWEATHER_API_KEY", "")
    if not api_key:
        return {"temp": None, "desc": "Chưa cấu hình API key", "icon": "🌤️", "source": "config"}
    lat = 21.0 if lat is None else lat
    lon = 105.8 if lon is None else lon
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=vi"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10)
            data = r.json()
    except Exception:
        return {"temp": None, "desc": "Lỗi kết nối", "icon": "🌤️", "source": "error"}
    return {
        "temp": data.get("main", {}).get("temp"),
        "desc": data.get("weather", [{}])[0].get("description", ""),
        "icon": data.get("weather", [{}])[0].get("icon", ""),
        "source": "api",
    }


# ── Admin: Users ─────────────────────────────────────────────────────────────

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_role("admin")), error: str = None):
    c = connect()
    try:
        users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
        projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name")]
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "users": users, "projects": projects,
        "error": _ADMIN_USER_ERRORS.get(error, ""),
    })


_ADMIN_USER_ERRORS = {
    "duplicate-username": "Username đã tồn tại, chọn tên khác.",
    "last-admin": "Không thể xoá admin cuối cùng của hệ thống.",
}


@app.post("/admin/users/create")
def admin_user_create(request: Request, user=Depends(require_role("admin")),
                     username: str = Form(...), password: str = Form(...),
                     full_name: str = Form(...), role: str = Form("viewer"),
                     phone: str = Form(""), email: str = Form(""),
                     telegram_chat_id: str = Form("")):
    username = username.strip()
    c = connect()
    try:
        existing = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
            projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name")]
            return templates.TemplateResponse(request, "admin_users.html", {
                "user": user, "users": users, "projects": projects,
                "error": _ADMIN_USER_ERRORS["duplicate-username"],
            }, status_code=400)
        c.execute("""INSERT INTO users (username, password_hash, full_name, role, phone, email, telegram_chat_id)
                     VALUES (?,?,?,?,?,?,?)""",
                  (username, hash_password(password), full_name, role, phone, email, telegram_chat_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/users", status_code=302)


# ── Admin: Labor & Machinery library ─────────────────────────────────────────

@app.get("/admin/library", response_class=HTMLResponse)
def admin_library(request: Request, user=Depends(require_role("admin"))):
    c = connect()
    try:
        labor = [dict(r) for r in c.execute("SELECT * FROM labor_types ORDER BY sort_order")]
        machinery = [dict(r) for r in c.execute("SELECT * FROM machinery_types ORDER BY sort_order")]
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_library.html", {
        "user": user, "labor": labor, "machinery": machinery,
    })


@app.post("/admin/library/labor/add")
def add_labor(request: Request, user=Depends(require_role("admin")),
              name: str = Form(...), unit: str = Form("người")):
    c = connect()
    try:
        c.execute("INSERT OR IGNORE INTO labor_types (name, unit) VALUES (?,?)", (name, unit))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


@app.post("/admin/library/machinery/add")
def add_machinery(request: Request, user=Depends(require_role("admin")),
                  name: str = Form(...), unit: str = Form("máy")):
    c = connect()
    try:
        c.execute("INSERT OR IGNORE INTO machinery_types (name, unit) VALUES (?,?)", (name, unit))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


@app.post("/admin/library/labor/{item_id}/edit")
def edit_labor(request: Request, item_id: int, user=Depends(require_role("admin")),
               name: str = Form(...), unit: str = Form("người")):
    c = connect()
    try:
        c.execute("UPDATE labor_types SET name=?, unit=? WHERE id=?", (name, unit, item_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


@app.post("/admin/library/labor/{item_id}/toggle")
def toggle_labor(request: Request, item_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        c.execute("UPDATE labor_types SET is_active = CASE WHEN COALESCE(is_active,1)=1 THEN 0 ELSE 1 END WHERE id=?", (item_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


@app.post("/admin/library/machinery/{item_id}/edit")
def edit_machinery(request: Request, item_id: int, user=Depends(require_role("admin")),
                   name: str = Form(...), unit: str = Form("máy")):
    c = connect()
    try:
        c.execute("UPDATE machinery_types SET name=?, unit=? WHERE id=?", (name, unit, item_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


@app.post("/admin/library/machinery/{item_id}/toggle")
def toggle_machinery(request: Request, item_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        c.execute("UPDATE machinery_types SET is_active = CASE WHEN COALESCE(is_active,1)=1 THEN 0 ELSE 1 END WHERE id=?", (item_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


# ── Admin: Assign projects to user ───────────────────────────────────────────

@app.get("/admin/users/{user_id}/projects", response_class=HTMLResponse)
def admin_user_projects(request: Request, user_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        target_row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target_row:
            raise HTTPException(status_code=404)
        target = dict(target_row)
        projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name")]
        assigned = {r["project_id"] for r in c.execute(
            "SELECT project_id FROM user_projects WHERE user_id=?", (user_id,))}
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_user_projects.html", {
        "user": user, "target": target, "projects": projects, "assigned": assigned,
    })


@app.post("/admin/users/{user_id}/projects")
async def admin_user_projects_save(request: Request, user_id: int, user=Depends(require_role("admin"))):
    form = await request.form()
    selected = set(int(x) for x in form.getlist("project_ids"))
    c = connect()
    try:
        c.execute("DELETE FROM user_projects WHERE user_id=?", (user_id,))
        for pid in selected:
            c.execute("INSERT INTO user_projects (user_id, project_id) VALUES (?,?)", (user_id, pid))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/admin/users/{user_id}/projects", status_code=302)

# ── CRUD: Edit Diary ──────────────────────────────────────────────────────────

@app.get("/projects/{project_id}/diary/{diary_id}/edit", response_class=HTMLResponse)
async def diary_edit(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_write_dep)]):
    c = connect()
    try:
        # Fetch raw rows and validate BEFORE dict(): a missing project or
        # diary must yield 404, not a TypeError from dict(None) leaking a
        # 500 through the global exception handler.
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        diary_row = c.execute("SELECT * FROM diaries WHERE id=?", (diary_id,)).fetchone()
        # Reject URL/project mismatches: the diary must belong to the project
        # in the path, otherwise a swapped project_id would render one
        # project's diary under another project's heading.
        if not project_row or not diary_row or diary_row["project_id"] != project_id:
            raise HTTPException(status_code=404)
        project = dict(project_row)
        diary = dict(diary_row)
        session = "morning" if datetime.now().hour < 12 else "afternoon"
        await ensure_weather(c, project_id, diary["date"], project.get("lat", 21.0), project.get("lon", 105.8), session)
        weather_rows = {r["session"]: dict(r) for r in c.execute(
            "SELECT session, temp, description AS desc, icon FROM project_weather_log "
            "WHERE project_id=? AND log_date=?", (project_id, diary["date"]))}
        _all_labor = [dict(r) for r in c.execute("SELECT * FROM labor_types ORDER BY sort_order")]
        _all_mach = [dict(r) for r in c.execute("SELECT * FROM machinery_types ORDER BY sort_order")]
        _el = {r["labor_type_id"] for r in c.execute("SELECT labor_type_id FROM diary_labor WHERE diary_id=?", (diary_id,))}
        _em = {r["machinery_type_id"] for r in c.execute("SELECT machinery_type_id FROM diary_machinery WHERE diary_id=?", (diary_id,))}
        labor_types = [t for t in _all_labor if t.get("is_active", 1) or t["id"] in _el]
        machinery_types = [t for t in _all_mach if t.get("is_active", 1) or t["id"] in _em]
        existing_labor = {r["labor_type_id"]: r["count"] for r in c.execute(
            "SELECT * FROM diary_labor WHERE diary_id=?", (diary_id,))}
        existing_machinery = {r["machinery_type_id"]: r["hours"] for r in c.execute(
            "SELECT * FROM diary_machinery WHERE diary_id=?", (diary_id,))}
        materials = [dict(r) for r in c.execute("SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute("SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute("SELECT * FROM diary_photos WHERE diary_id=?", (diary_id,))]
        existing_issues = _load_diary_issues(c, diary_id)
        # Backward-compat: a diary that was created before the child-table
        # migration still carries its single issue in the legacy ``diaries``
        # columns.  Prefill one pseudo-issue so the edit form does not render
        # an empty §7 and the operator's existing text is not lost.
        if not existing_issues:
            pseudo = _legacy_issue_dict(diary_row)
            if pseudo:
                existing_issues = [pseudo]
    finally:
        c.close()
    return templates.TemplateResponse(request, "diary_form.html", {
        "user": user, "project": project, "target_date": diary["date"],
        "labor_types": labor_types, "machinery_types": machinery_types,
        "prev_labor": existing_labor, "prev_machinery": existing_machinery,
        "prev_materials": materials, "prev_photos": photos,
        "diary": diary, "diary_id": diary_id, "existing_tasks": tasks,
        "existing_issues": existing_issues,
        # The form falls back to weather_default when a legacy diary has no
        # temperature; the edit route has no default to offer, but the key must
        # exist so the template never touches an undefined variable.
        "weather_default": {},
        "weather_morning": weather_rows.get("morning", {"temp": diary.get("weather_temp"), "desc": diary.get("weather_desc", "")}),
        "weather_afternoon": weather_rows.get("afternoon", {}),
    })


@app.post("/projects/{project_id}/diary/{diary_id}/delete")
def diary_delete(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_write_dep)]):
    # Snapshot rows BEFORE the diary is removed (FK cascade wipes diary_photos).
    c = connect()
    try:
        drow = c.execute("SELECT project_id FROM diaries WHERE id=?", (diary_id,)).fetchone()
        if not drow or drow["project_id"] != project_id:
            raise HTTPException(status_code=404)
        photos = [dict(r) for r in c.execute(
            "SELECT filename, size_bytes FROM diary_photos WHERE diary_id=?",
            (diary_id,)).fetchall()]
        c.execute("DELETE FROM diaries WHERE id=?", (diary_id,))
        c.commit()
    finally:
        c.close()
    # Quota is released only after the object is confirmed gone, so a failed
    # deletion never frees bytes that are still stored.
    released = 0
    for p in photos:
        if not p["filename"]:
            continue
        if r2.delete_file(p["filename"]):
            released += int(p["size_bytes"] or 0)
    r2.release(released)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── CRUD: Delete Project ─────────────────────────────────────────────────────

@app.post("/projects/{project_id}/delete")
def project_delete(request: Request, project_id: int, user=Depends(require_role("admin"))):
    # Collect diary ids + photo filenames BEFORE deleting so the stored
    # objects (R2/local) can be cleaned up; otherwise they leak.
    c = connect()
    try:
        diary_ids = [r["id"] for r in c.execute(
            "SELECT id FROM diaries WHERE project_id=?", (project_id,)).fetchall()]
        # Snapshot (filename, size_bytes) BEFORE the cascade wipes diary_photos.
        photos_by_diary = {}
        for did in diary_ids:
            photos_by_diary[did] = [dict(r) for r in c.execute(
                "SELECT filename, size_bytes FROM diary_photos WHERE diary_id=?",
                (did,)).fetchall()]
        # Remove child diaries (their children cascade via FK) before the project.
        for did in diary_ids:
            c.execute("DELETE FROM diaries WHERE id=?", (did,))
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        c.commit()
    finally:
        c.close()
    # Quota is released only for objects actually removed from storage.
    removed = set()
    for did in diary_ids:
        removed.update(r2.delete_diary_objects(did, [p["filename"] for p in photos_by_diary.get(did, [])]))
    released = 0
    for did in diary_ids:
        for p in photos_by_diary.get(did, []):
            if p["filename"] in removed:
                released += int(p["size_bytes"] or 0)
    r2.release(released)
    return RedirectResponse(url="/", status_code=302)


# ── CRUD: Delete User ────────────────────────────────────────────────────────

@app.post("/admin/users/{user_id}/delete")
def admin_user_delete(request: Request, user_id: int, user=Depends(require_role("admin"))):
    if user_id == user["id"]:
        return RedirectResponse(url="/admin/users", status_code=302)
    c = connect()
    try:
        target = c.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            return RedirectResponse(url="/admin/users", status_code=302)
        if target["role"] == "admin":
            admins = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
            if admins <= 1:
                return RedirectResponse(url="/admin/users?error=last-admin", status_code=302)
        # diaries.created_by has no ON DELETE action, so an author reference
        # would raise a FOREIGN KEY error: detach it before removing the user.
        c.execute("UPDATE diaries SET created_by=NULL WHERE created_by=?", (user_id,))
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/users", status_code=302)


# ── CRUD: Delete Recipient ───────────────────────────────────────────────────

@app.post("/projects/{project_id}/recipients/{recipient_id}/delete")
def recipient_delete(request: Request, project_id: int, recipient_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        # Scope the delete to the project in the URL so a mismatched id cannot
        # remove another project's recipient.
        c.execute("DELETE FROM recipients WHERE id=? AND project_id=?", (recipient_id, project_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


@app.post("/projects/{project_id}/recipients/add")
async def add_recipient(request: Request, project_id: int, user=Depends(require_role("admin"))):
    """Add one or several recipients from the multi-row project form.

    The template renders a row per recipient and clones it with JS, so several
    ``name``/``telegram_chat_id`` values arrive in one submit; reading the form
    directly keeps every row instead of binding only the last one.
    """
    form = await request.form()

    def pick(field, index, default=""):
        values = form.getlist(field)
        if index < len(values):
            value = values[index]
            if value is not None and str(value).strip():
                return str(value).strip()
        return default

    names = form.getlist("name")
    c = connect()
    try:
        project_row = c.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        for i, raw_name in enumerate(names):
            name = (raw_name or "").strip()
            if not name:
                continue
            telegram_type = pick("telegram_type", i, "account") or "account"
            if telegram_type not in ("account", "group"):
                telegram_type = "account"
            c.execute(
                """INSERT INTO recipients (project_id, name, email, phone, telegram_chat_id, telegram_type, role, frequency)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (project_id, name, pick("email", i), pick("phone", i),
                 pick("telegram_chat_id", i), telegram_type,
                 pick("role", i), pick("frequency", i, "daily") or "daily"))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── Change password ───────────────────────────────────────────────────────────

@app.get("/my-settings", response_class=HTMLResponse)
def my_settings_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(request, "my_settings.html", {"user": user})


@app.post("/my-settings")
def my_settings_save(request: Request, user=Depends(require_user),
                    telegram_chat_id: str = Form(""),
                    phone: str = Form(""), email: str = Form("")):
    c = connect()
    try:
        c.execute("UPDATE users SET telegram_chat_id=?, phone=?, email=? WHERE id=?",
                  (telegram_chat_id.strip(), phone.strip(), email.strip(), user["id"]))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/my-settings", status_code=302)


@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(request, "change_password.html", {"user": user})


@app.post("/change-password")
def change_password(request: Request, user=Depends(require_user),
                    old_password: str = Form(...), new_password: str = Form(...),
                    confirm: str = Form(...)):
    if new_password != confirm:
        return templates.TemplateResponse(request, "change_password.html",
                                         {"user": user, "error": "Mật khẩu xác nhận không khớp"})
    if len(new_password) < 6:
        return templates.TemplateResponse(request, "change_password.html",
                                         {"user": user, "error": "Mật khẩu mới phải ≥ 6 ký tự"})
    c = connect()
    try:
        row = c.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not verify_password(old_password, row["password_hash"]):
            return templates.TemplateResponse(request, "change_password.html",
                                             {"user": user, "error": "Mật khẩu cũ sai"})
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (hash_password(new_password), user["id"]))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/", status_code=302)


# ── Telegram send (manual trigger) ────────────────────────────────────────────

@app.post("/projects/{project_id}/diary/{diary_id}/send-telegram")
async def send_telegram(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_write_dep)]):
    from urllib.parse import quote
    from app.telegram_bot import send_diary_report
    # Telegram send is a project-scoped action: only callers assigned to the
    # diary's project (or admin) may trigger it.  The recipient set is
    # resolved inside send_diary_report; here we gate the caller's project
    # access so an unassigned user cannot fire a report to anyone.
    _c0 = connect()
    try:
        _drow = _c0.execute("SELECT project_id FROM diaries WHERE id=?", (diary_id,)).fetchone()
    finally:
        _c0.close()
    if not _drow or _drow["project_id"] != project_id:
        raise HTTPException(status_code=404)
    try:
        result = await send_diary_report(project_id, diary_id)
    except Exception as exc:
        from app.telegram_bot import sanitize_error
        return RedirectResponse(
            url=f"/projects/{project_id}?tg_error={quote(sanitize_error(exc))}", status_code=302)
    if result and not result.get("ok"):
        from app.telegram_bot import sanitize_error
        # Redirect with readable error message (never 500).  Include the
        # concise provider-error details so a bad chat id or missing R2
        # object can be told apart in the UI alert.
        err = str(result.get("error") or "Lỗi")
        details = result.get("details")
        if details:
            err = f"{err} | {details}"
        return RedirectResponse(
            url=f"/projects/{project_id}?tg_error={quote(sanitize_error(err))}",
            status_code=302)
    return RedirectResponse(url=f"/projects/{project_id}?tg_ok=1", status_code=302)


@app.post("/projects/{project_id}/diary/{diary_id}/save")
async def diary_edit_save(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_write_dep)]):
    import os as _os
    _os.makedirs(str(__import__("pathlib").Path(__file__).parent.parent / "uploads"), exist_ok=True)
    form = await request.form()
    # Reject a swapped project_id: the diary must belong to the project in
    # the path, otherwise one project's diary could be rewritten under
    # another project's URL.
    _c0 = connect()
    try:
        _drow = _c0.execute("SELECT project_id FROM diaries WHERE id=?", (diary_id,)).fetchone()
    finally:
        _c0.close()
    if not _drow or _drow["project_id"] != project_id:
        raise HTTPException(status_code=404)
    diary_date = _valid_diary_date(form.get("date"))
    if not diary_date:
        return HTMLResponse(
            "<h3>Ngày thi công không hợp lệ</h3>"
            "<p>Chọn ngày theo định dạng YYYY-MM-DD rồi lưu lại.</p>",
            status_code=400)
    weather_temp = _round_temp(form.get("weather_temp"))
    weather_desc = form.get("weather_desc")
    morning_temp = _weather_form_value(form, "weather_morning_temp", weather_temp)
    morning_desc = form.get("weather_morning_desc") or weather_desc
    afternoon_temp = _weather_form_value(form, "weather_afternoon_temp")
    afternoon_desc = form.get("weather_afternoon_desc") or ""
    foreman = form.get("foreman", "")
    notes = form.get("notes", "")
    plan_tomorrow = form.get("plan_tomorrow", "")
    issues = form.get("issues", "")
    issue_level = _norm_issue_level(form.get("issue_level", "none"))
    issue_status = form.get("issue_status", "")
    cause = _sanitize_text(form.get("cause", ""))
    related_party = _sanitize_text(form.get("related_party", ""))
    workflow_status = _norm_workflow(form.get("workflow_status", "none"))
    resolved_at = form.get("resolved_at", "") or None
    workflow_status, resolved_at = _resolve_date_logic(workflow_status, resolved_at)
    # Dynamic multi-issue payload (new path).  Falls back to the legacy
    # single-issue fields when the form does not submit issue_count.
    issues_payload = _parse_issue_payload(form)
    # Auto-calculate total workers from all labor_ fields
    total_workers = 0
    for key, val in form.items():
        if key.startswith("labor_") and key != "labor_ids":
            try:
                total_workers += int(val) if val and val.strip() else 0
            except ValueError:
                pass

    c = connect()
    try:
        # A diary may only move to a free date: UNIQUE(project_id, date) would
        # otherwise raise an IntegrityError and lose the whole edit.
        clash = c.execute(
            "SELECT id FROM diaries WHERE project_id=? AND date=? AND id<>?",
            (project_id, diary_date, diary_id)).fetchone()
        if clash:
            return HTMLResponse(
                "<h3>Ngày đã có nhật ký</h3>"
                "<p>Dự án đã có nhật ký cho ngày này. Chọn ngày khác hoặc sửa nhật ký đó.</p>",
                status_code=400)
        store_weather(c, project_id, diary_date, "morning", {"temp": morning_temp, "desc": morning_desc})
        store_weather(c, project_id, diary_date, "afternoon", {"temp": afternoon_temp, "desc": afternoon_desc})
        c.execute("""UPDATE diaries SET date=?, weather_temp=?, weather_desc=?, foreman=?,
                     total_workers=?, notes=?, plan_tomorrow=?, issues=?, issue_level=?, issue_status=?,
                     cause=?, related_party=?, workflow_status=?, resolved_at=?,
                     updated_at=datetime('now') WHERE id=?""",
                   (diary_date, morning_temp, morning_desc, foreman, total_workers, notes, plan_tomorrow,
                   issues, issue_level, issue_status, cause, related_party, workflow_status,
                   resolved_at, diary_id))
        for tbl in ["diary_labor", "diary_machinery", "diary_materials",
                    "diary_tasks", "diary_acceptance"]:
            c.execute(f"DELETE FROM {tbl} WHERE diary_id=?", (diary_id,))

        labor_ids_str = form.get("labor_ids", "")
        for lt_id in _parse_id_list([labor_ids_str]):
            try:
                count = int(form.get(f"labor_{lt_id}", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
                          (diary_id, lt_id, count))
        machinery_ids_str = form.get("machinery_ids", "")
        for mt_id in _parse_id_list([machinery_ids_str]):
            qty_str = form.get(f"machinery_{mt_id}", "0")
            try:
                qty = int(qty_str) if qty_str and qty_str.strip() else 0
            except (ValueError, TypeError):
                qty = 0
            if qty > 0:
                c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
                          (diary_id, mt_id, qty))
        mat_names = form.getlist("mat_name")
        mat_units = form.getlist("mat_unit")
        mat_qtys = form.getlist("mat_qty")
        for i, name in enumerate(mat_names):
            if name.strip():
                c.execute("INSERT INTO diary_materials (diary_id, name, unit, quantity) VALUES (?,?,?,?)",
                          (diary_id, name, mat_units[i] if i < len(mat_units) else "",
                           float(mat_qtys[i]) if i < len(mat_qtys) and mat_qtys[i] and mat_qtys[i].strip() else 0))
        # Tasks
        task_names = form.getlist("task_name")
        task_qtys = form.getlist("task_qty")
        task_units = form.getlist("task_unit")
        task_pcts = form.getlist("task_pct")
        for i, name in enumerate(task_names):
            if name.strip():
                c.execute("INSERT INTO diary_tasks (diary_id, task_name, quantity, unit, cumulative_pct) VALUES (?,?,?,?,?)",
                          (diary_id, name,
                           float(task_qtys[i]) if i < len(task_qtys) and task_qtys[i] and task_qtys[i].strip() else 0,
                           task_units[i] if i < len(task_units) else "",
                           float(task_pcts[i]) if i < len(task_pcts) and task_pcts[i] and task_pcts[i].strip() else 0))

        # Acceptance (nghiệm thu)
        acc_indices = set()
        for key in form.keys():
            if key.startswith("acceptance_") and "_item_name" in key:
                idx = key.split("_")[1]
                if idx.isdigit():
                    acc_indices.add(int(idx))
        
        for idx in sorted(acc_indices):
            item_name = form.get(f"acceptance_{idx}_item_name", "")
            if not item_name.strip():
                continue
            spec = form.get(f"acceptance_{idx}_spec", "")
            quantity = form.get(f"acceptance_{idx}_quantity", "")
            unit = form.get(f"acceptance_{idx}_unit", "")
            result = form.get(f"acceptance_{idx}_result", "passed")
            inspector = form.get(f"acceptance_{idx}_inspector", "")
            note = form.get(f"acceptance_{idx}_note", "")
            
            try:
                qty = float(quantity) if quantity and quantity.strip() else 0
            except (ValueError, TypeError):
                qty = 0
            
            c.execute("""INSERT INTO diary_acceptance (diary_id, item_name, spec, quantity, unit, result, note, inspector)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (diary_id, item_name, spec, qty, unit, result, note, inspector))

        prior_ids = [r["id"] for r in c.execute(
            "SELECT id FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()]
        from datetime import datetime as _dt2
        stamp = _dt2.now().strftime("%d/%m/%Y %H:%M")
        issue_photo_ids, new_photos_are_issue = _issue_photo_flags(form)
        photo_errors = []
        for f, caption in _extract_new_photos(form):
            fname, reserved = await _persist_photo(f, diary_id, stamp)
            if not fname:
                photo_errors.append(reserved or "Lỗi upload ảnh")
                continue
            kind = "issue" if new_photos_are_issue else "work"
            c.execute("INSERT INTO diary_photos (diary_id, filename, caption, size_bytes, photo_kind) VALUES (?,?,?,?,?)",
                      (diary_id, fname, caption if caption is not None else "", reserved, kind))
        if photo_errors:
            c.rollback()
            c.close()
            return HTMLResponse(
                "<h3>Lỗi upload ảnh</h3><ul>%s</ul>"
                % "".join("<li>%s</li>" % e for e in photo_errors),
                status_code=400)

        existing_ids = form.getlist("existing_photo_ids")
        existing_captions = form.getlist("existing_photo_captions")
        if "existing_photo_mode" in form:
            deferred_old = await _queue_photo_replacements(
                        c, diary_id, existing_ids, existing_captions, form, prior_ids)
        else:
            deferred_old = []

        for pid in issue_photo_ids:
            c.execute("UPDATE diary_photos SET photo_kind='issue' WHERE id=? AND diary_id=?",
                      (pid, diary_id))

        # Multi-issue child rows (new path).  Falls back to legacy single-issue
        # fields when the form does not submit issue_count.
        created_issues, alert_triggers = _save_diary_issues(c, diary_id, issues_payload)
        for issue in alert_triggers:
            _mark_issue_alerted(c, issue["id"])
        # Sync legacy diaries columns from the top issue so old readers keep working.
        if created_issues:
            top = created_issues[0]
            c.execute("UPDATE diaries SET issue_level=?, issue_status=?, cause=?, "
                      "related_party=?, issues=?, workflow_status=?, resolved_at=? WHERE id=?",
                      (top["level"], top["suggestion"], top["cause"], top["related_party"],
                       top["description"], top["workflow_status"], top["resolved_at"], diary_id))

        c.commit()
        _cleanup_replaced_files(deferred_old)

        for issue in alert_triggers:
            await _fire_issue_alert_for_issue(project_id, diary_id, issue)
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


@app.get("/projects/{project_id}/diary/{diary_id}/preview", response_class=HTMLResponse)
def diary_preview(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_access_dep)]):
    c = connect()
    try:
        # Fetch raw rows and validate BEFORE dict(): a missing diary or a
        # project/diary mismatch must yield 404, not a TypeError from
        # dict(None) leaking a 500 through the global exception handler.
        diary_row = c.execute("SELECT * FROM diaries WHERE id=?", (diary_id,)).fetchone()
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not diary_row or not project_row or diary_row["project_id"] != project_id:
            raise HTTPException(status_code=404)
        diary = dict(diary_row)
        project = dict(project_row)
        labor = [dict(r) for r in c.execute(
            """SELECT lt.name, dl.count FROM diary_labor dl
               JOIN labor_types lt ON dl.labor_type_id=lt.id WHERE dl.diary_id=?""", (diary_id,))]
        machinery = [dict(r) for r in c.execute(
            """SELECT mt.name, dm.hours FROM diary_machinery dm
               JOIN machinery_types mt ON dm.machinery_type_id=mt.id WHERE dm.diary_id=?""", (diary_id,))]
        materials = [dict(r) for r in c.execute("SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute("SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        acceptance = [dict(r) for r in c.execute("SELECT * FROM diary_acceptance WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute("SELECT * FROM diary_photos WHERE diary_id=?", (diary_id,))]
        issues = _load_diary_issues(c, diary_id)
        if not issues:
            pseudo = _legacy_issue_dict(diary)
            issues = [pseudo] if pseudo else []
        weather = {r["session"]: dict(r) for r in c.execute(
            "SELECT session, temp, description AS desc FROM project_weather_log "
            "WHERE project_id=? AND log_date=?", (project_id, diary["date"]))}
    finally:
        c.close()
    return templates.TemplateResponse(request, "diary_preview.html", {
        "user": user, "project": project, "diary": diary,
        "labor": labor, "machinery": machinery, "materials": materials,
        "tasks": tasks, "photos": photos, "issues": issues,
        "weather": weather,
    })


@app.get("/projects/{project_id}/diary/{diary_id}/pdf")
def diary_pdf(request: Request, project_id: int, diary_id: int, user: Annotated[dict, Depends(diary_access_dep)]):
    # The dependency already gates assignment on the diary's owning project,
    # but the path project_id must still match that project: otherwise a
    # caller assigned to project A can read project B's metadata by pointing
    # the URL at project B while keeping A's diary id.
    _c0 = connect()
    try:
        _drow = _c0.execute("SELECT project_id FROM diaries WHERE id=?", (diary_id,)).fetchone()
    finally:
        _c0.close()
    if not _drow or _drow["project_id"] != project_id:
        raise HTTPException(status_code=404)
    from app.export_pdf import generate_diary_pdf
    path = generate_diary_pdf(project_id, diary_id)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"nhatky_{diary_id}.pdf")
# ── Weekly Report ───────────────────────────────────────────────────────────

from datetime import datetime, timedelta

@app.get("/projects/{project_id}/weekly-report", response_class=HTMLResponse)
def weekly_report_page(request: Request, project_id: int,
                       user: Annotated[dict, Depends(project_write_dep)],
                       week_start: str = None):
    """Show weekly report page for a project."""
    if not week_start:
        today = datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(today, "%Y-%m-%d")
        week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    
    week_end = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    
    c = connect()
    try:
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        project = dict(project_row)
        
        diaries = [dict(r) for r in c.execute(
            "SELECT * FROM diaries WHERE project_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (project_id, week_start, week_end)).fetchall()]

        if diaries:
            diary_ids = [d["id"] for d in diaries]
            placeholders = ",".join("?" for _ in diary_ids)

            # Labor
            labor = [dict(r) for r in c.execute(
                f"""SELECT lt.name, SUM(dl.count) as total_count
                    FROM diary_labor dl
                    JOIN labor_types lt ON dl.labor_type_id = lt.id
                    WHERE dl.diary_id IN ({placeholders})
                    GROUP BY lt.name ORDER BY total_count DESC""",
                diary_ids).fetchall()]

            # Machinery
            machinery = [dict(r) for r in c.execute(
                f"""SELECT mt.name, SUM(dm.hours) as total_hours
                    FROM diary_machinery dm
                    JOIN machinery_types mt ON dm.machinery_type_id = mt.id
                    WHERE dm.diary_id IN ({placeholders})
                    GROUP BY mt.name ORDER BY total_hours DESC""",
                diary_ids).fetchall()]

            # Tasks
            tasks = [dict(r) for r in c.execute(
                f"""SELECT task_name, SUM(quantity) as total_qty, unit, MAX(cumulative_pct) as max_pct
                    FROM diary_tasks
                    WHERE diary_id IN ({placeholders})
                    GROUP BY task_name, unit ORDER BY max_pct DESC""",
                diary_ids).fetchall()]

            # Acceptance: aggregated in Python because GROUP_CONCAT is
            # SQLite-only and would break the PostgreSQL deployment.
            acc_rows = [dict(r) for r in c.execute(
                f"""SELECT result, inspector FROM diary_acceptance
                    WHERE diary_id IN ({placeholders})""", diary_ids).fetchall()]
            acc_by_result = {}
            for row in acc_rows:
                key = row.get("result") or "passed"
                bucket = acc_by_result.setdefault(key, {"result": key, "count": 0, "inspectors": []})
                bucket["count"] += 1
                inspector = (row.get("inspector") or "").strip()
                if inspector and inspector not in bucket["inspectors"]:
                    bucket["inspectors"].append(inspector)
            acceptance = [
                {"result": b["result"], "count": b["count"],
                 "inspectors": ", ".join(b["inspectors"])}
                for b in sorted(acc_by_result.values(), key=lambda x: x["count"], reverse=True)
            ]

            # Issues
            issues = [dict(r) for r in c.execute(
                f"""SELECT i.description, i.level, i.workflow_status, i.resolved_at,
                           d.date as diary_date
                    FROM diary_issues i
                    JOIN diaries d ON i.diary_id = d.id
                    WHERE i.diary_id IN ({placeholders})
                    AND i.workflow_status != 'resolved'
                    ORDER BY d.date DESC""",
                diary_ids).fetchall()]

            # Weather: averaged in Python for the same portability reason.
            weather_rows = [dict(r) for r in c.execute(
                """SELECT session, temp, description FROM project_weather_log
                   WHERE project_id = ? AND log_date BETWEEN ? AND ?""",
                (project_id, week_start, week_end)).fetchall()]
            weather_by_session = {}
            for row in weather_rows:
                key = row.get("session") or ""
                bucket = weather_by_session.setdefault(
                    key, {"session": key, "temps": [], "descriptions": []})
                if row.get("temp") is not None:
                    bucket["temps"].append(float(row["temp"]))
                if row.get("description"):
                    bucket["descriptions"].append(row["description"])
            weather = []
            for bucket in weather_by_session.values():
                temps = bucket["temps"]
                weather.append({
                    "session": bucket["session"],
                    "avg_temp": (sum(temps) / len(temps)) if temps else None,
                    "descriptions": ", ".join(bucket["descriptions"]),
                })
            weather.sort(key=lambda w: 0 if w["session"] == "morning" else 1)
        else:
            labor = machinery = tasks = acceptance = issues = weather = []
    finally:
        c.close()
    
    return templates.TemplateResponse(request, "weekly_report.html", {
        "user": user,
        "project": project,
        "diaries": diaries,
        "week_start": week_start,
        "week_end": week_end,
        "labor": labor,
        "machinery": machinery,
        "tasks": tasks,
        "acceptance": acceptance,
        "issues": issues,
        "weather": weather,
    })


@app.post("/projects/{project_id}/weekly-report/send")
async def weekly_report_send(request: Request, project_id: int,
                       user: Annotated[dict, Depends(project_write_dep)],
                       week_start: str = Form(...)):
    """Generate and send weekly report via Telegram."""
    from app.telegram_bot import send_message, get_bot_token
    
    week_end = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    
    c = connect()
    try:
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404)
        project = dict(project_row)
        
        # Get recipients for this project
        recipients_rows = c.execute(
            """SELECT telegram_chat_id FROM recipients WHERE project_id=? AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' AND frequency IN ('daily', 'weekly')""",
            (project_id,)
        ).fetchall()
        chat_ids = {r["telegram_chat_id"] for r in recipients_rows if r["telegram_chat_id"]}

        # Also get assigned users' telegram
        user_rows = c.execute(
            """SELECT u.telegram_chat_id FROM users u
               JOIN user_projects up ON u.id = up.user_id
               WHERE up.project_id=? AND u.telegram_chat_id IS NOT NULL AND u.telegram_chat_id != ''""",
            (project_id,)
        ).fetchall()
        for r in user_rows:
            if r["telegram_chat_id"]:
                chat_ids.add(r["telegram_chat_id"])
    finally:
        c.close()
    
    if not chat_ids:
        return {"ok": False, "error": "Không có người nhận Telegram được cấu hình"}
    
    # Generate report text
    report_lines = [
        f"BÁO CÁO TUẦN - {project['name']}",
        f"Tuần: {week_start} - {week_end}",
        ""
    ]
    
    # Generate report data
    c = connect()
    try:
        diaries = [dict(r) for r in c.execute(
            "SELECT * FROM diaries WHERE project_id=? AND date BETWEEN ? AND ? ORDER BY date",
            (project_id, week_start, week_end)).fetchall()]
        
        if diaries:
            diary_ids = [d["id"] for d in diaries]
            placeholders = ",".join("?" for _ in diary_ids)
            
            # Labor summary
            labor = c.execute(
                f"""SELECT lt.name, SUM(dl.count) as total_count
                    FROM diary_labor dl
                    JOIN labor_types lt ON dl.labor_type_id = lt.id
                    WHERE dl.diary_id IN ({placeholders})
                    GROUP BY lt.name ORDER BY total_count DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            if labor:
                report_lines.append("NHÂN LỰC:")
                for l in labor:
                    report_lines.append(f"  - {l['name']}: {l['total_count']} người")
                report_lines.append("")
            
            # Tasks
            tasks = c.execute(
                f"""SELECT task_name, SUM(quantity) as total_qty, unit
                    FROM diary_tasks
                    WHERE diary_id IN ({placeholders})
                    GROUP BY task_name, unit ORDER BY total_qty DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            if tasks:
                report_lines.append("CÔNG VIỆC HOÀN THÀNH:")
                for t in tasks:
                    report_lines.append(f"  - {t['task_name']}: {t['total_qty']} {t['unit']}")
                report_lines.append("")
            
            # Acceptance
            acceptance = c.execute(
                f"""SELECT result, COUNT(*) as count
                    FROM diary_acceptance
                    WHERE diary_id IN ({placeholders})
                    GROUP BY result ORDER BY count DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            if acceptance:
                report_lines.append("NGHIỆM THU:")
                for a in acceptance:
                    result_text = "Đạt" if a['result'] == 'passed' else ("Không đạt" if a['result'] == 'failed' else "Đạt có điều kiện")
                    report_lines.append(f"  - {result_text}: {a['count']} hạng mục")
                report_lines.append("")
            
            # Issues
            issues = c.execute(
                f"""SELECT i.description, i.level, i.workflow_status, d.date
                    FROM diary_issues i
                    JOIN diaries d ON i.diary_id = d.id
                    WHERE i.diary_id IN ({placeholders})
                    AND i.workflow_status != 'resolved'
                    ORDER BY d.date DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            if issues:
                report_lines.append("VẤN ĐỀ CẦN XỬ LÝ:")
                for i in issues:
                    level_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(i['level'], "")
                    report_lines.append(f"  {level_emoji} [{i['date']}] {i['description'][:60]} ({i['workflow_status']})")
                report_lines.append("")
    finally:
        c.close()
    
    report_text = "\n".join(report_lines)
    
    # Send to all recipients
    token = get_bot_token()
    if not token:
        return {"ok": False, "error": "Chưa cấu hình Bot Token"}
    
    sent = 0
    errors = []
    for chat_id in sorted(chat_ids):
        try:
            from app.telegram_bot import send_message
            result = await send_message(chat_id, report_text)
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
            continue
        if isinstance(result, dict) and result.get("ok"):
            sent += 1
        else:
            error = result.get("error") if isinstance(result, dict) else None
            errors.append(f"{chat_id}: {error or 'gửi thất bại'}")

    return {"ok": sent > 0, "sent": sent, "errors": errors,
            "message": f"Đã gửi báo cáo tuần cho {sent} người nhận"}


@app.get("/projects/{project_id}/weekly-report/pdf")
def weekly_report_pdf(request: Request, project_id: int,
                      user: Annotated[dict, Depends(project_write_dep)],
                      week_start: str = None):
    """Generate weekly report PDF."""
    if not week_start:
        today = datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(today, "%Y-%m-%d")
        week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    
    week_end = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    
    # For now, redirect to the HTML page with a print-to-PDF instruction
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/projects/{project_id}/weekly-report?week_start={week_start}", status_code=302)