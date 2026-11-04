"""Main FastAPI application — Nhật ký thi công."""
import os
import sys
import json
from pathlib import Path
from datetime import date, timedelta

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.db import connect, init_db
from app.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_user, require_role,
)

app = FastAPI(title="Nhật ký thi công")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(BASE_DIR / "uploads")), name="uploads")

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


@app.on_event("startup")
def startup():
    init_db()


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
    token = create_access_token({"user_id": row["id"], "role": row["role"]})
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, max_age=60*60*24*7)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("access_token")
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
        n_diaries = c.execute("SELECT COUNT(*) FROM diaries").fetchone()[0]
        today = date.today().isoformat()
        has_today = False
        if projects:
            has_today = c.execute(
                "SELECT COUNT(*) FROM diaries WHERE date=? AND project_id IN ({})".format(
                    ",".join("?" * len(projects))),
                [today] + [p["id"] for p in projects],
            ).fetchone()[0] > 0
    finally:
        c.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "projects": projects, "n_diaries": n_diaries,
        "has_today": has_today, "today": today,
    })


# ── Projects ─────────────────────────────────────────────────────────────────

@app.get("/projects/new", response_class=HTMLResponse)
def project_new_page(request: Request, user=Depends(require_role("admin"))):
    return templates.TemplateResponse(request, "project_form.html", {"user": user, "project": None})


@app.post("/projects/new")
def project_create(request: Request, user=Depends(require_role("admin")),
                   name: str = Form(...), address: str = Form(""), investor: str = Form(""),
                   contractor: str = Form(""), supervisor: str = Form(""),
                   start_date: str = Form(""), end_date: str = Form("")):
    c = connect()
    try:
        c.execute("""INSERT INTO projects (name, address, investor, contractor, supervisor, start_date, end_date)
                     VALUES (?,?,?,?,?,?,?)""",
                  (name, address, investor, contractor, supervisor, start_date, end_date))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/", status_code=302)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int, user=Depends(require_user)):
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
def diary_new(request: Request, project_id: int, date_str: str = None, user=Depends(require_user)):
    c = connect()
    try:
        project = dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        labor_types = [dict(r) for r in c.execute("SELECT * FROM labor_types ORDER BY sort_order")]
        machinery_types = [dict(r) for r in c.execute("SELECT * FROM machinery_types ORDER BY sort_order")]
        # Auto-fill from previous day
        target_date = date_str or date.today().isoformat()
        prev = c.execute(
            "SELECT * FROM diaries WHERE project_id=? AND date < ? ORDER BY date DESC LIMIT 1",
            (project_id, target_date)).fetchone()
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
            prev_photos = [dict(r) for r in c.execute(
                "SELECT * FROM diary_photos WHERE diary_id=?", (prev["id"],))]
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
    })


@app.post("/projects/{project_id}/diary/save")
async def diary_save(request: Request, project_id: int, user=Depends(require_user)):
    form = await request.form()
    diary_date = form.get("date")
    weather_temp = form.get("weather_temp")
    weather_desc = form.get("weather_desc")
    foreman = form.get("foreman", "")
    notes = form.get("notes", "")
    issues = form.get("issues", "")
    issue_level = form.get("issue_level", "none")
    issue_status = form.get("issue_status", "none")
    total_workers = int(form.get("total_workers", 0) or 0)

    c = connect()
    try:
        # Upsert diary
        existing = c.execute(
            "SELECT id FROM diaries WHERE project_id=? AND date=?", (project_id, diary_date)).fetchone()
        if existing:
            diary_id = existing["id"]
            c.execute("""UPDATE diaries SET weather_temp=?, weather_desc=?, foreman=?,
                         total_workers=?, notes=?, issues=?, issue_level=?, issue_status=?,
                         updated_at=datetime('now') WHERE id=?""",
                      (weather_temp, weather_desc, foreman, total_workers, notes, issues,
                       issue_level, issue_status, diary_id))
            # Clear old children
            for tbl in ["diary_labor", "diary_machinery", "diary_materials", "diary_tasks"]:
                c.execute(f"DELETE FROM {tbl} WHERE diary_id=?", (diary_id,))
        else:
            cur = c.execute("""INSERT INTO diaries (project_id, date, weather_temp, weather_desc,
                             foreman, total_workers, notes, issues, issue_level, issue_status, created_by)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (project_id, diary_date, weather_temp, weather_desc, foreman,
                             total_workers, notes, issues, issue_level, issue_status, user["id"]))
            diary_id = cur.lastrowid

        # Labor
        for key, val in form.items():
            if key.startswith("labor_"):
                lt_id = int(key.split("_")[1])
                count = int(val or 0)
                if count > 0:
                    c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
                              (diary_id, lt_id, count))
        # Machinery
        for key, val in form.items():
            if key.startswith("machinery_"):
                mt_id = int(key.split("_")[1])
                hours = float(val or 0)
                if hours > 0:
                    c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
                              (diary_id, mt_id, hours))
        # Materials
        mat_names = form.getlist("mat_name")
        mat_units = form.getlist("mat_unit")
        mat_qtys = form.getlist("mat_qty")
        for i, name in enumerate(mat_names):
            if name.strip():
                c.execute("INSERT INTO diary_materials (diary_id, name, unit, quantity) VALUES (?,?,?,?)",
                          (diary_id, name, mat_units[i] if i < len(mat_units) else "",
                           float(mat_qtys[i] or 0) if i < len(mat_qtys) else 0))
        # Tasks
        task_names = form.getlist("task_name")
        task_qtys = form.getlist("task_qty")
        task_units = form.getlist("task_unit")
        task_pcts = form.getlist("task_pct")
        for i, name in enumerate(task_names):
            if name.strip():
                c.execute("INSERT INTO diary_tasks (diary_id, task_name, quantity, unit, cumulative_pct) VALUES (?,?,?,?,?)",
                          (diary_id, name,
                           float(task_qtys[i] or 0) if i < len(task_qtys) else 0,
                           task_units[i] if i < len(task_units) else "",
                           float(task_pcts[i] or 0) if i < len(task_pcts) else 0))

        # Photos
        files = form.getlist("photos")
        for f in files:
            if f and f.filename:
                ext = Path(f.filename).suffix
                fname = f"diary_{diary_id}_{len(os.listdir(UPLOAD_DIR))}{ext}"
                fpath = UPLOAD_DIR / fname
                content = await f.read()
                with open(fpath, "wb") as fh:
                    fh.write(content)
                c.execute("INSERT INTO diary_photos (diary_id, filename) VALUES (?,?)", (diary_id, fname))

        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── Weather API proxy ────────────────────────────────────────────────────────

@app.get("/api/weather")
async def weather_proxy(lat: float = 21.0, lon: float = 105.8):
    import httpx
    api_key = os.getenv("OPENWEATHER_API_KEY", "")
    if not api_key:
        return {"temp": None, "desc": "Chưa cấu hình API key", "icon": "🌤️"}
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=vi"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        data = r.json()
    return {
        "temp": data.get("main", {}).get("temp"),
        "desc": data.get("weather", [{}])[0].get("description", ""),
        "icon": data.get("weather", [{}])[0].get("icon", ""),
    }


# ── Admin: Users ─────────────────────────────────────────────────────────────

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_role("admin"))):
    c = connect()
    try:
        users = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
        projects = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name")]
    finally:
        c.close()
    return templates.TemplateResponse(request, "admin_users.html", {
        "user": user, "users": users, "projects": projects,
    })


@app.post("/admin/users/create")
def admin_user_create(request: Request, user=Depends(require_role("admin")),
                     username: str = Form(...), password: str = Form(...),
                     full_name: str = Form(...), role: str = Form("viewer"),
                     phone: str = Form(""), email: str = Form(""),
                     telegram_chat_id: str = Form("")):
    c = connect()
    try:
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
                  name: str = Form(...), unit: str = Form("giờ")):
    c = connect()
    try:
        c.execute("INSERT OR IGNORE INTO machinery_types (name, unit) VALUES (?,?)", (name, unit))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/admin/library", status_code=302)


# ── Recipients ───────────────────────────────────────────────────────────────

@app.post("/projects/{project_id}/recipients/add")
def add_recipient(request: Request, project_id: int, user=Depends(require_role("admin")),
                  name: str = Form(...), email: str = Form(""), phone: str = Form(""),
                  telegram_chat_id: str = Form(""), role: str = Form(""), frequency: str = Form("daily")):
    c = connect()
    try:
        c.execute("""INSERT INTO recipients (project_id, name, email, phone, telegram_chat_id, role, frequency)
                     VALUES (?,?,?,?,?,?,?)""",
                  (project_id, name, email, phone, telegram_chat_id, role, frequency))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── Telegram send (manual trigger) ───────────────────────────────────────────

@app.post("/projects/{project_id}/diary/{diary_id}/send-telegram")
async def send_telegram(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    from app.telegram_bot import send_diary_report
    await send_diary_report(project_id, diary_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── PDF Export ───────────────────────────────────────────────────────────────

@app.get("/projects/{project_id}/diary/{diary_id}/pdf")
def diary_pdf(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    from app.export_pdf import generate_diary_pdf
    path = generate_diary_pdf(project_id, diary_id)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"nhatky_{diary_id}.pdf")
