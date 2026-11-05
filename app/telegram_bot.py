"""Telegram reporting module."""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect

# Telegram Bot API photo limit: 10 MB for sendPhoto (input file).
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024

# Explicit timeouts for the Telegram HTTP client.  A slow R2 read or a
# congested Telegram endpoint used to hang the whole diary send and turn
# it into a 500; these values bound connect / read / write / pool and the
# per-request wall-clock so a single bad photo cannot block the rest.
HTTP_CONNECT_TIMEOUT = 5.0
HTTP_READ_TIMEOUT = 15.0
HTTP_WRITE_TIMEOUT = 30.0
HTTP_POOL_TIMEOUT = 5.0
HTTP_REQUEST_TIMEOUT = 60.0


def _compress_photo(photo_bytes: bytes, max_bytes: int = TELEGRAM_PHOTO_MAX_BYTES) -> bytes:
    """Resize / recompress an oversized image so it fits Telegram's limit.

    Pillow is used only when the input actually exceeds the limit; small
    photos pass through untouched.  The output is always a valid JPEG so
    the Telegram ``sendPhoto`` multipart upload never rejects the file.
    """
    if not photo_bytes or len(photo_bytes) <= max_bytes:
        return photo_bytes
    try:
        from PIL import Image
    except Exception:
        return photo_bytes
    try:
        img = Image.open(io.BytesIO(photo_bytes))
    except Exception:
        return photo_bytes
    img = img.convert("RGB")
    # Shrink the longest edge until the JPEG is under the limit.
    max_edge = max(img.size)
    if max_edge > 1280:
        ratio = 1280 / max_edge
        img = img.resize((max(1, int(img.size[0] * ratio)), max(1, int(img.size[1] * ratio))))
    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data
    return data


def get_bot_token():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from app.db import connect
            c = connect()
            row = c.execute("SELECT value FROM settings WHERE key='telegram_bot_token'").fetchone()
            c.close()
            if row and row["value"]:
                token = row["value"]
        except Exception:
            pass
    return token

BOT_TOKEN = get_bot_token()


async def send_message(chat_id: str, text: str):
    BOT_TOKEN = get_bot_token()
    if not BOT_TOKEN or not chat_id:
        return
    import httpx
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def _vn_now():
    """Current time in Asia/Ho_Chi_Minh, used so reminders fire on VN wall-clock."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    except Exception:
        # Fallback for very old runtimes without zoneinfo.
        import datetime as _d
        return _d.datetime.utcnow() + _d.timedelta(hours=7)


async def send_reminder(project_id: int, force: bool = False) -> dict:
    """Nag every recipient of *project_id* to create today's diary.

    A project is reminded only when its ``reminder_enabled`` flag is on, its
    reminder time has come (or *force* is set), today's diary does not yet
    exist, and we have not already reminded today — the ``reminder_log`` table
    dedupes per (project, date) so a repeated tick cannot spam the chat.

    "Today" and the wall-clock comparison use Asia/Ho_Chi_Minh so the
    schedule matches the Vietnamese working day regardless of server TZ.

    Returns a summary dict; never raises.
    """
    from datetime import date as _date
    now = _vn_now()
    today = now.date().isoformat()
    c = connect()
    try:
        project = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            return {"ok": False, "error": "Dự án không tồn tại"}
        project = dict(project)
        if not project.get("reminder_enabled", 1):
            return {"ok": False, "error": "Đã tắt nhắc nhở"}
        if not force:
            reminder_time = project.get("reminder_time") or "08:00"
            try:
                hh, mm = (int(x) for x in str(reminder_time).split(":")[:2])
            except (ValueError, TypeError):
                hh, mm = 8, 0
            if now.hour < hh or (now.hour == hh and now.minute < mm):
                return {"ok": False, "error": "Chưa đến giờ nhắc nhở"}
        # Dedupe: already sent today?
        existing = c.execute(
            "SELECT 1 FROM reminder_log WHERE project_id=? AND log_date=?",
            (project_id, today)).fetchone()
        if existing and not force:
            return {"ok": False, "error": "Hôm nay đã nhắc rồi"}
        # Today's diary already exists → nothing to nag about.
        has_today = c.execute(
            "SELECT 1 FROM diaries WHERE project_id=? AND date=?",
            (project_id, today)).fetchone()
        if has_today:
            c.execute(
                "INSERT OR IGNORE INTO reminder_log (project_id, log_date) VALUES (?,?)",
                (project_id, today))
            c.commit()
            return {"ok": False, "error": "Đã có nhật ký hôm nay"}
        recipients = [dict(r) for r in c.execute(
            "SELECT * FROM recipients WHERE project_id=? AND telegram_chat_id IS NOT NULL "
            "AND telegram_chat_id != ''", (project_id,))]
        users = [dict(r) for r in c.execute(
            """SELECT u.* FROM users u JOIN user_projects up ON u.id=up.user_id
               WHERE up.project_id=? AND u.telegram_chat_id IS NOT NULL
               AND u.telegram_chat_id != ''""", (project_id,))]
        admins = [dict(r) for r in c.execute(
            "SELECT * FROM users WHERE role='admin' AND telegram_chat_id IS NOT NULL "
            "AND telegram_chat_id != ''")]
    finally:
        c.close()

    chat_ids = set()
    for r in recipients:
        chat_ids.add(r["telegram_chat_id"])
    for u in users:
        chat_ids.add(u["telegram_chat_id"])
    for a in admins:
        chat_ids.add(a["telegram_chat_id"])

    if not chat_ids:
        return {"ok": False, "error": "Chưa có người nhận Telegram"}

    msg = (
        f"🔔 <b>Nhắc nhở tạo nhật ký</b>\n"
        f"📅 Ngày {now.strftime('%d/%m/%Y')}: dự án <b>{project['name']}</b>"
        f" ({project.get('address', '')}) chưa có nhật ký thi công.\n"
        f"Vui lòng nhập nhật ký tại: /projects/{project_id}/diary/new"
    )
    ok = 0
    fail = 0
    errors = []
    for chat_id in chat_ids:
        try:
            await send_message(chat_id, msg)
            ok += 1
        except Exception as exc:
            fail += 1
            errors.append(f"{chat_id}: {exc}")

    c = connect()
    try:
        c.execute(
            "INSERT OR IGNORE INTO reminder_log (project_id, log_date) VALUES (?,?)",
            (project_id, today))
        c.commit()
    finally:
        c.close()

    summary = {"ok": ok > 0, "sent_to": len(chat_ids), "ok_count": ok, "fail_count": fail}
    if errors:
        summary["errors"] = errors[:10]
    return summary


async def _reminder_tick(force: bool = False) -> dict:
    """Run reminders for every project that has the flag on.

    Called by the scheduler loop and by the manual admin trigger.  A single
    project failing must not abort the rest.
    """
    today = _vn_now().date().isoformat()
    c = connect()
    try:
        rows = c.execute(
            "SELECT id, name, COALESCE(reminder_enabled,1) AS enabled FROM projects"
        ).fetchall()
    finally:
        c.close()
    results = {}
    for p in rows:
        pid = p["id"]
        if not p["enabled"]:
            continue
        try:
            results[str(pid)] = await send_reminder(pid, force=force)
        except Exception as exc:
            results[str(pid)] = {"ok": False, "error": f"L exception: {exc}"}
    return {"ok": True, "date": today, "projects": results}


async def send_photo(chat_id: str, photo_path: str = "", caption: str = "",
                     photo_bytes: bytes | None = None):
    """Send a photo to Telegram.

    ``photo_bytes`` takes precedence when supplied (R2-backed photos); the
    function falls back to reading ``photo_path`` from the local filesystem
    for legacy callers.

    Returns a dict ``{"ok": bool, "error": str | None}`` so callers can
    count successes / failures without wrapping this function in try/except.
    """
    token = get_bot_token()
    if not token or not chat_id:
        return {"ok": False, "error": "Thiếu Bot Token hoặc Chat ID"}
    if photo_bytes is None and photo_path:
        try:
            with open(photo_path, "rb") as f:
                photo_bytes = f.read()
        except OSError:
            return {"ok": False, "error": "Không đọc được file ảnh"}
    if not photo_bytes:
        return {"ok": False, "error": "Không có dữ liệu ảnh"}

    # Resize / recompress oversized images before upload so a single huge
    # photo cannot blow Telegram's limit (or the request body) and stall
    # the remaining photos in the same diary.
    photo_bytes = _compress_photo(photo_bytes)

    import httpx
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                HTTP_REQUEST_TIMEOUT,
                connect=HTTP_CONNECT_TIMEOUT,
                read=HTTP_READ_TIMEOUT,
                write=HTTP_WRITE_TIMEOUT,
                pool=HTTP_POOL_TIMEOUT,
            )
        ) as client:
            resp = await client.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
            )
        if resp.status_code >= 400:
            return {"ok": False,
                    "error": f"Telegram trả về HTTP {resp.status_code}"}
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if not payload.get("ok", False):
            return {"ok": False,
                    "error": f"Telegram từ chối: {payload.get('description', 'lỗi không rõ')}"}
        return {"ok": True, "error": None}
    except httpx.TimeoutException as exc:
        return {"ok": False, "error": f"Timeout Telegram: {exc}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Lỗi mạng Telegram: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Lỗi không xác định: {exc}"}


async def send_document(chat_id: str, doc_path: str, caption: str = ""):
    BOT_TOKEN = get_bot_token()
    if not BOT_TOKEN or not chat_id:
        return
    import httpx
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    async with httpx.AsyncClient() as client:
        with open(doc_path, "rb") as f:
            await client.post(url, data={"chat_id": chat_id, "caption": caption},
                             files={"document": f})


async def send_diary_report(project_id: int, diary_id: int):
    token = get_bot_token()
    if not token:
        print("ERROR: No bot token configured")
        return {"ok": False, "error": "Chưa cấu hình Bot Token"}
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
        photos = [dict(r) for r in c.execute(
            "SELECT filename, caption FROM diary_photos WHERE diary_id=?", (diary_id,))]
        # Get recipients with telegram_chat_id
        recipients = [dict(r) for r in c.execute(
            "SELECT * FROM recipients WHERE project_id=? AND telegram_chat_id IS NOT NULL AND telegram_chat_id != ''",
            (project_id,))]
        # Also get users assigned to this project with telegram_chat_id
        users = [dict(r) for r in c.execute(
            """SELECT u.* FROM users u JOIN user_projects up ON u.id=up.user_id
               WHERE up.project_id=? AND u.telegram_chat_id IS NOT NULL AND u.telegram_chat_id != ''""",
            (project_id,))]
        # If admin, also include
        admins = [dict(r) for r in c.execute(
            "SELECT * FROM users WHERE role='admin' AND telegram_chat_id IS NOT NULL AND telegram_chat_id != ''")]
    finally:
        c.close()

    # Collect all chat_ids to send to
    chat_ids = set()
    for r in recipients:
        chat_ids.add(r["telegram_chat_id"])
    for u in users:
        chat_ids.add(u["telegram_chat_id"])
    for a in admins:
        chat_ids.add(a["telegram_chat_id"])

    if not chat_ids:
        print("ERROR: No recipients with telegram_chat_id")
        return {"ok": False, "error": "Chưa có người nhận với Telegram Chat ID"}

    # Build message
    lines = [
        f"📋 <b>Nhật ký thi công — {project['name']}</b>",
        f"📅 Ngày: {diary['date']}",
        f"🌤️ Thời tiết: {diary['weather_temp']}°C — {diary['weather_desc']}",
        f"👷 Tổng nhân lực: {diary['total_workers']} người",
        "",
        "<b>Nhân lực:</b>",
    ]
    for l in labor:
        if l["count"] > 0:
            lines.append(f"  • {l['name']}: {l['count']} người")
    lines.append("")
    lines.append("<b>Máy móc:</b>")
    for m in machinery:
        if m["hours"] > 0:
            lines.append(f"  • {m['name']}: {m['hours']} máy")
    if diary.get("notes"):
        lines.append(f"\n📝 Ghi chú: {diary['notes']}")
    if diary.get("issues") and diary["issues"].strip():
        level = {"none": "", "low": "🟢", "medium": "🟡", "high": "🔴"}.get(diary.get("issue_level", "none"), "")
        lines.append(f"\n⚠️ Vướng mắc {level}: {diary['issues']}")
        lines.append(f"Kiến nghị: {diary.get('issue_status', '')}")
    lines.append(f"\n📸 Ảnh: {len(photos)} ảnh")

    msg = "\n".join(lines)
    upload_dir = Path(__file__).parent.parent / "uploads"
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from app import r2

    photos_sent = 0
    photos_failed = 0
    photo_errors: list[str] = []
    chat_ok = 0
    chat_fail = 0

    for chat_id in chat_ids:
        try:
            await send_message(chat_id, msg)
            chat_ok += 1
        except Exception as exc:
            chat_fail += 1
            photo_errors.append(f"chat {chat_id}: {exc}")

        for p in photos[:10]:
            data = r2.read_file(p["filename"])
            if data is None:
                # Legacy local fallback
                local = upload_dir / p["filename"]
                if local.exists():
                    data = local.read_bytes()
            if not data:
                photos_failed += 1
                photo_errors.append(f"{p['filename']}: không đọc được dữ liệu")
                continue
            try:
                result = await send_photo(chat_id, photo_bytes=data,
                                          caption=p.get("caption", ""))
            except Exception as exc:
                # Defensive: a raised exception must not abort the loop.
                result = {"ok": False, "error": f"L exception: {exc}"}
            if result.get("ok"):
                photos_sent += 1
            else:
                photos_failed += 1
                photo_errors.append(
                    f"{p['filename']}: {result.get('error', 'lỗi')}")

    summary = {
        "ok": True,
        "sent_to": len(chat_ids),
        "chat_ok": chat_ok,
        "chat_fail": chat_fail,
        "photos_sent": photos_sent,
        "photos_failed": photos_failed,
    }
    if photo_errors:
        summary["photo_errors"] = photo_errors[:20]
    # Return ok=False on any chat or photo failure with concise error message
    if chat_fail > 0 or photos_failed > 0:
        summary["ok"] = False
        summary["error"] = f"Đã gửi tới {chat_ok}/{len(chat_ids)} người chat, {photos_sent} ảnh thành công; {chat_fail} chat lỗi, {photos_failed} ảnh lỗi"
    return summary
