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

from app.db import connect, init_db, IS_PG
from app.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_user, require_role,
)

app = FastAPI(title="Nhật ký thi công")

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    print("GLOBAL ERROR:", exc)
    print(traceback.format_exc())
    from fastapi.responses import HTMLResponse
    return HTMLResponse(f"<pre>{traceback.format_exc()}</pre>", status_code=500)

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
        n_diaries = c.execute("SELECT COUNT(*) AS cnt FROM diaries").fetchone()["cnt"]
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
    })


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


@app.post("/projects/{project_id}/diary/save")
async def diary_save(request: Request, project_id: int, user=Depends(require_user)):
    import traceback
    try:
        form = await request.form()
    except Exception as e:
        print("Form parse error:", e, traceback.format_exc())
        raise
    diary_date = form.get("date")
    weather_temp = form.get("weather_temp")
    if weather_temp == "":
        weather_temp = None
    else:
        try:
            weather_temp = float(weather_temp)
        except (ValueError, TypeError):
            weather_temp = None
    weather_desc = form.get("weather_desc")
    foreman = form.get("foreman", "")
    notes = form.get("notes", "")
    issues = form.get("issues", "")
    issue_level = form.get("issue_level", "none")
    issue_status = form.get("issue_status", "none")
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
                             VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id""" if __import__("app.db", fromlist=["IS_PG"]).IS_PG else """INSERT INTO diaries (project_id, date, weather_temp, weather_desc,
                             foreman, total_workers, notes, issues, issue_level, issue_status, created_by)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (project_id, diary_date, weather_temp, weather_desc, foreman,
                             total_workers, notes, issues, issue_level, issue_status, user["id"]))
            if __import__("app.db", fromlist=["IS_PG"]).IS_PG:
                diary_id = cur.fetchone()["id"]
            else:
                diary_id = cur.lastrowid

        # Labor (dropdown approach)
        labor_ids_str = form.get("labor_ids", "")
        for lt_id_str in labor_ids_str.split(","):
            lt_id_str = lt_id_str.strip()
            if not lt_id_str:
                continue
            lt_id = int(lt_id_str)
            count = int(form.get(f"labor_{lt_id}", 0) or 0)
            if count > 0:
                c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
                          (diary_id, lt_id, count))
        # Machinery (dropdown approach)
        machinery_ids_str = form.get("machinery_ids", "")
        for mt_id_str in machinery_ids_str.split(","):
            mt_id_str = mt_id_str.strip()
            if not mt_id_str:
                continue
            mt_id = int(mt_id_str)
            hours_str = form.get(f"machinery_{mt_id}", "0")
            hours = float(hours_str) if hours_str and hours_str.strip() else 0.0
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

        # Capture existing photo IDs before inserting new ones
        existing_photo_ids_before = [r["id"] for r in c.execute("SELECT id FROM diary_photos WHERE diary_id=?", (diary_id,))]

        # Photos with captions
        files = form.getlist("photos")
        captions = form.getlist("photo_captions")
        for i, f in enumerate(files):
            if f and f.filename:
                ext = Path(f.filename).suffix
                fname = f"diary_{diary_id}_{len(os.listdir(UPLOAD_DIR))}{ext}"
                import os as _os
                _os.makedirs(str(UPLOAD_DIR), exist_ok=True)
                fpath = UPLOAD_DIR / fname
                file_content = await f.read()
                if not file_content:
                    continue
                with open(fpath, "wb") as fh:
                    fh.write(file_content)
                # Add timemark (non-fatal)
                try:
                    from datetime import datetime
                    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
                    _add_timemark(str(fpath), stamp)
                except Exception as te:
                    print("Timemark error:", te)
                caption = captions[i] if i < len(captions) else ""
                c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
                          (diary_id, fname, caption))

        # Update/delete existing photos
        existing_ids = form.getlist("existing_photo_ids")
        existing_captions = form.getlist("existing_photo_captions")
        kept_ids = [int(pid) for pid in existing_ids if pid]
        # Delete photos not in kept list
        for pid in existing_photo_ids_before:
            if pid not in kept_ids:
                # Also delete file
                import os as _os
                row = c.execute("SELECT filename FROM diary_photos WHERE id=?", (pid,)).fetchone()
                if row:
                    fpath = _os.path.join(_os.path.dirname(__file__), "..", "uploads", row["filename"])
                    try:
                        _os.remove(fpath)
                    except Exception:
                        pass
                c.execute("DELETE FROM diary_photos WHERE id=?", (pid,))
        # Update captions for kept photos
        for i, pid in enumerate(existing_ids):
            if pid and int(pid) in kept_ids:
                cap = existing_captions[i] if i < len(existing_captions) else ""
                c.execute("UPDATE diary_photos SET caption=? WHERE id=?", (cap, int(pid)))

        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── Weather API proxy ────────────────────────────────────────────────────────

# ── Admin: Settings ───────────────────────────────────────────────────────────

@app.get("/api/test-telegram")
async def test_telegram():
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
        return {"ok": False, "error": str(e)}


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


@app.get("/api/weather")
async def weather_proxy(lat: float = 21.0, lon: float = 105.8, date: str = None):
    import httpx
    from datetime import date as d, datetime
    # Only fetch weather if diary date is today (free API limitation)
    target = date or d.today().isoformat()
    if target != d.today().isoformat():
        return {"temp": None, "desc": "Nhập thủ công (ngày khác hôm nay)", "icon": ""}
    api_key = os.getenv("OPENWEATHER_API_KEY", "")
    try:
        c = connect()
        row = c.execute("SELECT value FROM settings WHERE key='openweather_api_key'").fetchone()
        c.close()
        if row and row["value"]:
            api_key = row["value"]
    except Exception:
        pass
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


# ── Admin: Assign projects to user ───────────────────────────────────────────

@app.get("/admin/users/{user_id}/projects", response_class=HTMLResponse)
def admin_user_projects(request: Request, user_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        target = dict(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
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
def diary_edit(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    c = connect()
    try:
        project = dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        diary = dict(c.execute("SELECT * FROM diaries WHERE id=?", (diary_id,)).fetchone())
        labor_types = [dict(r) for r in c.execute("SELECT * FROM labor_types ORDER BY sort_order")]
        machinery_types = [dict(r) for r in c.execute("SELECT * FROM machinery_types ORDER BY sort_order")]
        existing_labor = {r["labor_type_id"]: r["count"] for r in c.execute(
            "SELECT * FROM diary_labor WHERE diary_id=?", (diary_id,))}
        existing_machinery = {r["machinery_type_id"]: r["hours"] for r in c.execute(
            "SELECT * FROM diary_machinery WHERE diary_id=?", (diary_id,))}
        materials = [dict(r) for r in c.execute("SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute("SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute("SELECT * FROM diary_photos WHERE diary_id=?", (diary_id,))]
    finally:
        c.close()
    return templates.TemplateResponse(request, "diary_form.html", {
        "user": user, "project": project, "target_date": diary["date"],
        "labor_types": labor_types, "machinery_types": machinery_types,
        "prev_labor": existing_labor, "prev_machinery": existing_machinery,
        "prev_materials": materials, "prev_photos": photos,
        "diary": diary, "diary_id": diary_id, "existing_tasks": tasks,
    })


@app.post("/projects/{project_id}/diary/{diary_id}/delete")
def diary_delete(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    c = connect()
    try:
        c.execute("DELETE FROM diaries WHERE id=?", (diary_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


# ── CRUD: Delete Project ─────────────────────────────────────────────────────

@app.post("/projects/{project_id}/delete")
def project_delete(request: Request, project_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url="/", status_code=302)


# ── CRUD: Delete User ────────────────────────────────────────────────────────

@app.post("/admin/users/{user_id}/delete")
def admin_user_delete(request: Request, user_id: int, user=Depends(require_role("admin"))):
    if user_id == user["id"]:
        return RedirectResponse(url="/admin/users", status_code=302)
    c = connect()
    try:
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
        c.execute("DELETE FROM recipients WHERE id=?", (recipient_id,))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


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
async def send_telegram(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    from app.telegram_bot import send_diary_report
    result = await send_diary_report(project_id, diary_id)
    if result and not result.get("ok"):
        # Redirect with error message
        return RedirectResponse(url=f"/projects/{project_id}?tg_error={result.get('error', 'Lỗi')}", status_code=302)
    return RedirectResponse(url=f"/projects/{project_id}?tg_ok=1", status_code=302)


@app.post("/projects/{project_id}/diary/{diary_id}/save")
async def diary_edit_save(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    import os as _os
    _os.makedirs(str(__import__("pathlib").Path(__file__).parent.parent / "uploads"), exist_ok=True)
    form = await request.form()
    diary_date = form.get("date", "")
    weather_temp = form.get("weather_temp")
    if weather_temp == "":
        weather_temp = None
    else:
        try:
            weather_temp = float(weather_temp)
        except (ValueError, TypeError):
            weather_temp = None
    weather_desc = form.get("weather_desc")
    foreman = form.get("foreman", "")
    notes = form.get("notes", "")
    issues = form.get("issues", "")
    issue_level = form.get("issue_level", "none")
    issue_status = form.get("issue_status", "none")
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
        c.execute("""UPDATE diaries SET date=?, weather_temp=?, weather_desc=?, foreman=?,
                     total_workers=?, notes=?, issues=?, issue_level=?, issue_status=?,
                     updated_at=datetime('now') WHERE id=?""",
                  (diary_date, weather_temp, weather_desc, foreman, total_workers, notes,
                   issues, issue_level, issue_status, diary_id))
        for tbl in ["diary_labor", "diary_machinery", "diary_materials", "diary_tasks"]:
            c.execute(f"DELETE FROM {tbl} WHERE diary_id=?", (diary_id,))

        labor_ids_str = form.get("labor_ids", "")
        for lt_id_str in labor_ids_str.split(","):
            lt_id_str = lt_id_str.strip()
            if not lt_id_str:
                continue
            lt_id = int(lt_id_str)
            count = int(form.get(f"labor_{lt_id}", 0) or 0)
            if count > 0:
                c.execute("INSERT INTO diary_labor (diary_id, labor_type_id, count) VALUES (?,?,?)",
                          (diary_id, lt_id, count))
        machinery_ids_str = form.get("machinery_ids", "")
        for mt_id_str in machinery_ids_str.split(","):
            mt_id_str = mt_id_str.strip()
            if not mt_id_str:
                continue
            mt_id = int(mt_id_str)
            hours_str = form.get(f"machinery_{mt_id}", "0")
            hours = float(hours_str) if hours_str and hours_str.strip() else 0.0
            if hours > 0:
                c.execute("INSERT INTO diary_machinery (diary_id, machinery_type_id, hours) VALUES (?,?,?)",
                          (diary_id, mt_id, hours))
        mat_names = form.getlist("mat_name")
        mat_units = form.getlist("mat_unit")
        mat_qtys = form.getlist("mat_qty")
        for i, name in enumerate(mat_names):
            if name.strip():
                c.execute("INSERT INTO diary_materials (diary_id, name, unit, quantity) VALUES (?,?,?,?)",
                          (diary_id, name, mat_units[i] if i < len(mat_units) else "",
                           float(mat_qtys[i]) if i < len(mat_qtys) and mat_qtys[i] and mat_qtys[i].strip() else 0))
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

        files = form.getlist("photos")
        captions = form.getlist("photo_captions")
        for i, f in enumerate(files):
            if f and f.filename:
                ext = __import__("pathlib").Path(f.filename).suffix
                fname = f"diary_{diary_id}_{len(__import__('os').listdir(str(__import__('pathlib').Path(__file__).parent.parent / 'uploads')))}{ext}"
                upload_dir = __import__("pathlib").Path(__file__).parent.parent / "uploads"
                __import__("os").makedirs(str(upload_dir), exist_ok=True)
                fpath = upload_dir / fname
                file_content = await f.read()
                if not file_content:
                    continue
                with open(fpath, "wb") as fh:
                    fh.write(file_content)
                try:
                    from datetime import datetime
                    stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
                    _add_timemark(str(fpath), stamp)
                except Exception as te:
                    print("Timemark error:", te)
                caption = captions[i] if i < len(captions) else ""
                c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
                          (diary_id, fname, caption))

        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)

@app.get("/projects/{project_id}/diary/{diary_id}/preview", response_class=HTMLResponse)
def diary_preview(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    c = connect()
    try:
        diary = dict(c.execute("SELECT * FROM diaries WHERE id=?", (diary_id,)).fetchone())
        project = dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        labor = [dict(r) for r in c.execute(
            """SELECT lt.name, dl.count FROM diary_labor dl
               JOIN labor_types lt ON dl.labor_type_id=lt.id WHERE dl.diary_id=?""", (diary_id,))]
        machinery = [dict(r) for r in c.execute(
            """SELECT mt.name, dm.hours FROM diary_machinery dm
               JOIN machinery_types mt ON dm.machinery_type_id=mt.id WHERE dm.diary_id=?""", (diary_id,))]
        materials = [dict(r) for r in c.execute("SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute("SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute("SELECT * FROM diary_photos WHERE diary_id=?", (diary_id,))]
    finally:
        c.close()
    return templates.TemplateResponse(request, "diary_preview.html", {
        "user": user, "project": project, "diary": diary,
        "labor": labor, "machinery": machinery, "materials": materials,
        "tasks": tasks, "photos": photos,
    })


@app.get("/projects/{project_id}/diary/{diary_id}/pdf")
def diary_pdf(request: Request, project_id: int, diary_id: int, user=Depends(require_user)):
    from app.export_pdf import generate_diary_pdf
    path = generate_diary_pdf(project_id, diary_id)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"nhatky_{diary_id}.pdf")
