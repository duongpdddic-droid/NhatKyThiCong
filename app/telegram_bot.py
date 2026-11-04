"""Telegram reporting module."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect

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


async def send_photo(chat_id: str, photo_path: str, caption: str = ""):
    BOT_TOKEN = get_bot_token()
    if not BOT_TOKEN or not chat_id:
        return
    import httpx
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    async with httpx.AsyncClient() as client:
        with open(photo_path, "rb") as f:
            await client.post(url, data={"chat_id": chat_id, "caption": caption},
                             files={"photo": f})


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
            lines.append(f"  • {m['name']}: {m['hours']} giờ")
    if diary.get("notes"):
        lines.append(f"\n📝 Ghi chú: {diary['notes']}")
    if diary.get("issues") and diary["issues"].strip():
        level = {"none": "", "low": "🟢", "medium": "🟡", "high": "🔴"}.get(diary.get("issue_level", "none"), "")
        lines.append(f"\n⚠️ Vướng mắc {level}: {diary['issues']}")
        lines.append(f"Kiến nghị: {diary.get('issue_status', '')}")
    lines.append(f"\n📸 Ảnh: {len(photos)} ảnh")

    msg = "\n".join(lines)
    upload_dir = Path(__file__).parent.parent / "uploads"

    for chat_id in chat_ids:
        await send_message(chat_id, msg)
        for p in photos[:10]:
            await send_photo(chat_id, str(upload_dir / p["filename"]),
                             caption=p.get("caption", ""))
    return {"ok": True, "sent_to": len(chat_ids)}

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
            lines.append(f"  • {m['name']}: {m['hours']} giờ")
    if diary.get("notes"):
        lines.append(f"\n📝 Ghi chú: {diary['notes']}")
    if diary.get("issues") and diary["issues"].strip():
        level = {"none": "", "low": "🟢", "medium": "🟡", "high": "🔴"}.get(diary.get("issue_level", "none"), "")
        lines.append(f"\n⚠️ Vướng mắc {level}: {diary['issues']}")
        lines.append(f"Kiến nghị: {diary.get('issue_status', '')}")
    lines.append(f"\n📸 Ảnh: {len(photos)} ảnh")

    msg = "\n".join(lines)
    for r in recipients:
        if r.get("telegram_chat_id"):
            await send_message(r["telegram_chat_id"], msg)
            # Send photos
            upload_dir = Path(__file__).parent.parent / "uploads"
            for p in photos[:10]:
                await send_photo(r["telegram_chat_id"], str(upload_dir / p["filename"]))
            # Send PDF if exists
            # (PDF generation handled separately)
