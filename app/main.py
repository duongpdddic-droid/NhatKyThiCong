"""Main FastAPI application — Nhật ký thi công."""
import os
import sys
import json
import re
import uuid
from pathlib import Path
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
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
    get_current_user, require_user, require_role,
)
from app import r2

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


@app.on_event("startup")
def startup():
    init_db()
    import threading
    threading.Thread(target=_reminder_loop, daemon=True).start()


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


@app.get("/projects/{project_id}/edit", response_class=HTMLResponse)
def project_edit_page(request: Request, project_id: int, user=Depends(require_role("admin"))):
    c = connect()
    try:
        project = dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
    finally:
        c.close()
    return templates.TemplateResponse(request, "project_form.html", {"user": user, "project": project})


@app.post("/projects/{project_id}/edit")
def project_update(request: Request, project_id: int, user=Depends(require_role("admin")),
                   name: str = Form(...), address: str = Form(""), investor: str = Form(""),
                   contractor: str = Form(""), supervisor: str = Form(""),
                   start_date: str = Form(""), end_date: str = Form("")):
    c = connect()
    try:
        c.execute("""UPDATE projects SET name=?, address=?, investor=?, contractor=?,
                     supervisor=?, start_date=?, end_date=? WHERE id=?""",
                  (name, address, investor, contractor, supervisor, start_date, end_date, project_id))
        c.commit()
    finally:
        c.close()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=302)


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
        labor_types = [dict(r) for r in c.execute("SELECT * FROM labor_types WHERE COALESCE(is_active,1)=1 ORDER BY sort_order")]
        machinery_types = [dict(r) for r in c.execute("SELECT * FROM machinery_types WHERE COALESCE(is_active,1)=1 ORDER BY sort_order")]
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


async def _persist_photo(file_obj, diary_id: int, stamp: str) -> str | None:
    """Read an uploaded file, apply the timemark, and store it.

    Returns the stored filename, or None when the upload is empty.  Storage
    goes through ``r2.save_file`` so the bytes land in R2 when configured and
    on the local filesystem otherwise.  The timemark is applied to a temp file
    because Pillow works on paths; the temp file is removed afterwards.
    """
    content = await file_obj.read()
    if not content:
        return None
    ext = Path(file_obj.filename).suffix or ".jpg"
    fname = f"diary_{diary_id}_{uuid.uuid4().hex}{ext}"
    tmp = UPLOAD_DIR / f".tmp_{fname}"
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(content)
        try:
            _add_timemark(str(tmp), stamp)
        except Exception as te:
            print("Timemark error:", te)
        r2.save_file(fname, tmp.read_bytes())
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return fname


async def _queue_photo_replacements(c, diary_id, existing_ids, existing_captions, form, prior_ids=None):
    """Apply existing-photo changes (delete / replace / caption) safely.

    Replacements are written to storage first and only committed to the DB
    (replacing the old filename) *before* the transaction commits; the old
    object is removed only after a successful commit. Returns a list of old
    filenames deferred for cleanup.

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
    rows = c.execute("SELECT id, filename FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()
    for row in rows:
        pid = int(row["id"])
        # Never delete a photo that was just inserted in this request.
        if prior_set is not None and pid not in prior_set:
            continue
        if pid not in kept_ids:
            c.execute("DELETE FROM diary_photos WHERE id=? AND diary_id=?", (pid, diary_id))
            deferred_old.append(row["filename"])
    for i, pid in enumerate(existing_ids):
        if not pid:
            continue
        pid = int(pid)
        row = c.execute("SELECT filename, diary_id, caption FROM diary_photos WHERE id=? AND diary_id=?", (pid, diary_id)).fetchone()
        if not row:
            continue
        # Pick the first non-empty uploaded file for this photo id (gallery
        # and camera share the same per-id field name).
        files = form.getlist(f"existing_photo_replace_{pid}")
        f = next((x for x in files if x and getattr(x, "filename", "")), None)
        submitted_caption = existing_captions[i] if i < len(existing_captions) else ""
        if f:
            fname = await _persist_photo(f, row["diary_id"], stamp)
            if fname:
                cap = submitted_caption if submitted_caption.strip() else (row["caption"] or "")
                c.execute("UPDATE diary_photos SET filename=?, caption=? WHERE id=? AND diary_id=?", (fname, cap, pid, diary_id))
                deferred_old.append(row["filename"])
                continue
        cap = submitted_caption if submitted_caption.strip() else (row["caption"] or "")
        c.execute("UPDATE diary_photos SET caption=? WHERE id=? AND diary_id=?", (cap, pid, diary_id))
    return deferred_old


def _cleanup_replaced_files(deferred_old):
    """Remove old objects only after the new ones are safely on storage."""
    for old_filename in deferred_old:
        if old_filename:
            r2.delete_file(old_filename)


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

        # Photos with captions
        prior_ids = [r["id"] for r in c.execute(
            "SELECT id FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()]
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%d/%m/%Y %H:%M")
        for f, caption in _extract_new_photos(form):
            fname = await _persist_photo(f, diary_id, stamp)
            if not fname:
                continue
            c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
                      (diary_id, fname, caption if caption is not None else ""))

        # Update/delete/replace existing photos
        existing_ids = form.getlist("existing_photo_ids")
        existing_captions = form.getlist("existing_photo_captions")
        if "existing_photo_mode" in form:
            deferred_old = await _queue_photo_replacements(
                        c, diary_id, existing_ids, existing_captions, form, prior_ids)
        else:
            deferred_old = []

        c.commit()
        _cleanup_replaced_files(deferred_old)
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
        photos = [r["filename"] for r in c.execute(
            "SELECT filename FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()]
        c.execute("DELETE FROM diaries WHERE id=?", (diary_id,))
        c.commit()
    finally:
        c.close()
    r2.delete_diary_objects(diary_id, photos)
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
        photos_by_diary = {}
        for did in diary_ids:
            photos_by_diary[did] = [r["filename"] for r in c.execute(
                "SELECT filename FROM diary_photos WHERE diary_id=?", (did,)).fetchall()]
        # Remove child diaries (their children cascade via FK) before the project.
        for did in diary_ids:
            c.execute("DELETE FROM diaries WHERE id=?", (did,))
        c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        c.commit()
    finally:
        c.close()
    for did in diary_ids:
        r2.delete_diary_objects(did, photos_by_diary.get(did, []))
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
                  telegram_chat_id: str = Form(""), telegram_type: str = Form("account"),
                  role: str = Form(""), frequency: str = Form("daily")):
    # Validate recipient type server-side; default to account.
    if telegram_type not in ("account", "group"):
        telegram_type = "account"
    c = connect()
    try:
        c.execute("""INSERT INTO recipients (project_id, name, email, phone, telegram_chat_id, telegram_type, role, frequency)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (project_id, name, email, phone, telegram_chat_id.strip(), telegram_type, role, frequency))
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
    from urllib.parse import quote
    from app.telegram_bot import send_diary_report
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

        prior_ids = [r["id"] for r in c.execute(
            "SELECT id FROM diary_photos WHERE diary_id=?", (diary_id,)).fetchall()]
        from datetime import datetime as _dt2
        stamp = _dt2.now().strftime("%d/%m/%Y %H:%M")
        for f, caption in _extract_new_photos(form):
            fname = await _persist_photo(f, diary_id, stamp)
            if not fname:
                continue
            c.execute("INSERT INTO diary_photos (diary_id, filename, caption) VALUES (?,?,?)",
                      (diary_id, fname, caption if caption is not None else ""))

        existing_ids = form.getlist("existing_photo_ids")
        existing_captions = form.getlist("existing_photo_captions")
        if "existing_photo_mode" in form:
            deferred_old = await _queue_photo_replacements(
                        c, diary_id, existing_ids, existing_captions, form, prior_ids)
        else:
            deferred_old = []

        c.commit()
        _cleanup_replaced_files(deferred_old)
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
