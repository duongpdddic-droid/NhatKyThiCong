"""Telegram reporting module."""
import asyncio
import html as _html
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect

# Telegram Bot API photo limit: 10 MB for sendPhoto (input file).
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024
# Practical target size for normalized photos (keeps multipart writes fast).
TELEGRAM_PHOTO_TARGET_BYTES = 2 * 1024 * 1024
# Maximum edge (px) for normalized photos before upload.
TELEGRAM_PHOTO_MAX_EDGE = 1600
# Telegram sendMessage text limit.
TELEGRAM_MESSAGE_MAX_CHARS = 4096

# Explicit timeouts for the Telegram HTTP client.  A slow R2 read or a
# congested Telegram endpoint used to hang the whole diary send and turn
# it into a 500; these values bound connect / read / write / pool and the
# per-request wall-clock so a single bad photo cannot block the rest.
HTTP_CONNECT_TIMEOUT = 10.0
HTTP_READ_TIMEOUT = 60.0
HTTP_WRITE_TIMEOUT = 90.0
HTTP_POOL_TIMEOUT = 10.0
HTTP_REQUEST_TIMEOUT = 180.0


def _compress_photo(photo_bytes: bytes, max_bytes: int = TELEGRAM_PHOTO_TARGET_BYTES) -> bytes:
    """Normalize a real image to a practical small JPEG before upload.

    Phone photos below Telegram's 10 MB limit can still be large/slow enough to
    trigger ``httpx.WriteTimeout`` while writing the multipart body, so every
    real image is resized to at most ``TELEGRAM_PHOTO_MAX_EDGE`` on its longest
    side and re-encoded as JPEG targeting ``max_bytes`` (default 2 MB).  Non-image
    or undecodable inputs are returned untouched so legacy/unknown payloads are
    not corrupted.
    """
    if not photo_bytes:
        return photo_bytes
    try:
        from PIL import Image
    except Exception:
        return photo_bytes
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        img.load()
    except Exception:
        return photo_bytes
    try:
        img = img.convert("RGB")
    except Exception:
        return photo_bytes

    # Shrink the longest edge to a practical size for upload.
    max_edge = max(img.size)
    if max_edge > TELEGRAM_PHOTO_MAX_EDGE:
        ratio = TELEGRAM_PHOTO_MAX_EDGE / max_edge
        img = img.resize(
            (max(1, int(img.size[0] * ratio)),
             max(1, int(img.size[1] * ratio))),
            Image.LANCZOS,
        )

    # Re-encode, stepping quality down until under the target (or min quality).
    for quality in (85, 75, 65, 55, 45, 35):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data

    # Still too big: keep halving dimensions until it fits or we bottom out.
    while max(img.size) > 320:
        w, h = img.size
        img = img.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=35, optimize=True)
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


def sanitize_error(value, token=None):
    """Return a concise, secret-free representation of *value*.

    Bot tokens (and other configured credentials) are redacted so provider
    errors can be surfaced to the UI without leaking secrets.  The result is
    also truncated to keep redirect/query strings and alerts readable.
    """
    text = "" if value is None else str(value)
    if token is None:
        try:
            token = get_bot_token()
        except Exception:
            token = ""
    secrets = [token]
    try:
        secrets.append(os.getenv("TELEGRAM_BOT_TOKEN", ""))
    except Exception:
        pass
    try:
        secrets.append(os.getenv("R2_ACCESS_KEY_ID", ""))
        secrets.append(os.getenv("R2_SECRET_ACCESS_KEY", ""))
    except Exception:
        pass
    for secret in secrets:
        # Real credentials are long tokens; a bare substring replace is safe
        # for them.  Skip short secrets (a leaked test env var "s" or "k")
        # because replacing a single letter would mangle ordinary words.
        if secret and len(secret) >= 2:
            text = text.replace(secret, "[REDACTED]")
    text = " ".join(text.split())
    if len(text) > 240:
        text = text[:237] + "..."
    return text


# Backwards/alternative names used by callers and tests.
sanitize_exception = sanitize_error
sanitize_token = sanitize_error
redact_token = sanitize_error
redact_secrets = sanitize_error
_redact_secrets = sanitize_error
_sanitize_error = sanitize_error
redact = sanitize_error
sanitize = sanitize_error


BOT_TOKEN = get_bot_token()


async def send_message(chat_id: str, text: str):
    """Send a text message. Returns ``{"ok", "error"}`` (never raises).

    Telegram returns HTTP 200 with ``{"ok": false, ...}`` for API-level
    failures, which httpx does *not* turn into an exception, so the JSON
    payload is checked explicitly here.
    """
    token = get_bot_token()
    if not token or not chat_id:
        return {"ok": False, "error": "Thiếu Bot Token hoặc Chat ID"}
    import httpx
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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
                url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        if resp.status_code >= 400:
            description = None
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    description = payload.get("description")
            except Exception:
                description = None
            if description:
                return {"ok": False,
                        "error": sanitize_error(
                            f"Telegram trả về HTTP {resp.status_code}: {description}")}
            return {"ok": False,
                    "error": f"Telegram trả về HTTP {resp.status_code}"}
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if not payload.get("ok", False):
            return {"ok": False,
                    "error": sanitize_error(f"Telegram từ chối: {payload.get('description', 'lỗi không rõ')}")}
        return {"ok": True, "error": None}
    except httpx.TimeoutException as exc:
        return {"ok": False, "error": sanitize_error(f"Timeout Telegram: {exc}")}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": sanitize_error(f"Lỗi mạng Telegram: {exc}")}
    except Exception as exc:
        return {"ok": False, "error": sanitize_error(f"Lỗi không xác định: {exc}")}


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
        f"📅 Ngày {now.strftime('%d/%m/%Y')}: dự án <b>{_html.escape(str(project.get('name','')))}</b>"
        f" ({_html.escape(str(project.get('address','')))}) chưa có nhật ký thi công.\n"
        f"Vui lòng nhập nhật ký tại: /projects/{project_id}/diary/new"
    )
    ok = 0
    fail = 0
    errors = []
    for chat_id in chat_ids:
        try:
            res = await send_message(chat_id, msg)
            if res and res.get("ok"):
                ok += 1
            else:
                fail += 1
                errors.append(f"{chat_id}: {(res or {}).get('error', 'lỗi')}")
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

    # Normalize the image off the event loop so Pillow's CPU-bound
    # decode/resize/encode work cannot stall other requests.
    photo_bytes = await asyncio.to_thread(_compress_photo, photo_bytes)

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
            description = None
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    description = payload.get("description")
            except Exception:
                description = None
            if description:
                return {"ok": False,
                        "error": sanitize_error(
                            f"Telegram trả về HTTP {resp.status_code}: {description}")}
            return {"ok": False,
                    "error": f"Telegram trả về HTTP {resp.status_code}"}
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if not payload.get("ok", False):
            return {"ok": False,
                    "error": sanitize_error(f"Telegram từ chối: {payload.get('description', 'lỗi không rõ')}")}
        return {"ok": True, "error": None}
    except httpx.TimeoutException as exc:
        return {"ok": False, "error": sanitize_error(f"Timeout Telegram: {exc}")}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": sanitize_error(f"Lỗi mạng Telegram: {exc}")}
    except Exception as exc:
        return {"ok": False, "error": sanitize_error(f"Lỗi không xác định: {exc}")}


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


async def _send_diary_report_impl(project_id: int, diary_id: int):
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

    # Build message (escape user-controlled values for HTML parse_mode).
    def _esc(v):
        return _html.escape(str(v) if v is not None else "", quote=False)

    def _trunc(v, n=500):
        s = str(v) if v is not None else ""
        return (s[:n] + "…") if len(s) > n else s

    project_name = _esc(project.get("name", ""))
    weather_desc = _esc(diary.get("weather_desc", ""))
    weather_temp = _esc(diary.get("weather_temp", ""))
    total_workers = _esc(diary.get("total_workers", 0))
    notes = _esc(_trunc(diary.get("notes", ""), 1000))
    issues = _esc(_trunc(diary.get("issues", ""), 1000))
    issue_status = _esc(_trunc(diary.get("issue_status", ""), 500))
    cause = _esc(_trunc(diary.get("cause", ""), 300))
    related_party = _esc(_trunc(diary.get("related_party", ""), 200))
    workflow = _esc(diary.get("workflow_status", ""))
    resolved_at = _esc(diary.get("resolved_at", ""))
    level = {"none": "", "low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        diary.get("issue_level", "none"), "")
    level = _esc(level)

    lines = [
        f"📋 <b>Nhật ký thi công — {project_name}</b>",
        f"📅 Ngày: {_esc(diary.get('date', ''))}",
        f"🌤️ Thời tiết: {weather_temp}°C — {weather_desc}",
        f"👷 Tổng nhân lực: {total_workers} người",
        "",
        "<b>Nhân lực:</b>",
    ]
    for l in labor:
        if l.get("count", 0) > 0:
            lines.append(f"  • {_esc(l.get('name', ''))}: {l.get('count')} người")
    lines.append("")
    lines.append("<b>Máy móc:</b>")
    for m in machinery:
        if m.get("hours", 0) > 0:
            lines.append(f"  • {_esc(m.get('name', ''))}: {m.get('hours')} máy")
    if notes:
        lines.append(f"\n📝 Ghi chú: {notes}")
    if issues and str(diary.get("issues", "")).strip():
        lines.append(f"\n⚠️ Vướng mắc {level}: {issues}")
        if cause:
            lines.append(f"🔍 Nguyên nhân: {cause}")
        if related_party:
            lines.append(f"👤 Liên quan: {related_party}")
        if issue_status:
            lines.append(f"Kiến nghị: {issue_status}")
        if workflow:
            lines.append(f"Trạng thái: {workflow}")
        if resolved_at:
            lines.append(f"Ngày giải quyết: {resolved_at}")
    lines.append(f"\n📸 Ảnh: {len(photos)} ảnh")

    msg = "\n".join(lines)
    # Hard safety: Telegram sendMessage text must not exceed 4096 chars.
    if len(msg) > TELEGRAM_MESSAGE_MAX_CHARS:
        msg = msg[:TELEGRAM_MESSAGE_MAX_CHARS - 1] + "…"
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
            msg_result = await send_message(chat_id, msg)
        except Exception as exc:
            msg_result = {"ok": False, "error": f"L exception: {exc}"}
        if msg_result and msg_result.get("ok"):
            chat_ok += 1
        else:
            chat_fail += 1
            photo_errors.append(
                f"chat {chat_id}: {sanitize_error((msg_result or {}).get('error', 'lỗi'))}")

        for p in photos[:10]:
            filename = p.get("filename")
            read_error = None
            try:
                data = r2.read_file(filename)
                if data is None:
                    # Legacy local fallback
                    local = upload_dir / filename
                    if local.exists():
                        data = local.read_bytes()
            except Exception as exc:
                data = None
                read_error = exc
            if not data:
                photos_failed += 1
                if read_error is not None:
                    photo_errors.append(
                        f"chat {chat_id} {filename}: lỗi đọc file: {sanitize_error(read_error)}")
                else:
                    photo_errors.append(
                        f"chat {chat_id} {filename}: không đọc được dữ liệu")
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
                    f"chat {chat_id} {filename}: {sanitize_error(result.get('error', 'lỗi'))}")

    summary = {
        "ok": True,
        "sent_to": len(chat_ids),
        "chat_ok": chat_ok,
        "chat_fail": chat_fail,
        "photos_sent": photos_sent,
        "photos_failed": photos_failed,
    }
    # Sanitize every collected provider error before it leaves this function.
    photo_errors = [sanitize_error(e) for e in photo_errors]

    if photo_errors:
        summary["photo_errors"] = photo_errors[:20]
        # Concise, secret-free details built from the first provider errors so
        # callers/UI can distinguish a bad chat id from a missing R2 object.
        summary["details"] = " | ".join(photo_errors[:5])
    # Return ok=False on any chat or photo failure with concise error message
    if chat_fail > 0 or photos_failed > 0:
        summary["ok"] = False
        summary["error"] = sanitize_error(
            f"Đã gửi tới {chat_ok}/{len(chat_ids)} người chat, {photos_sent} ảnh thành công; {chat_fail} chat lỗi, {photos_failed} ảnh lỗi")
    return summary


async def send_diary_report(project_id: int, diary_id: int):
    """Public entry point: never raises; returns a summary dict."""
    try:
        return await _send_diary_report_impl(project_id, diary_id)
    except Exception as exc:
        return {"ok": False, "error": sanitize_error(f"Lỗi gửi báo cáo: {exc}")}
