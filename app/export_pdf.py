"""PDF export for construction diary."""
import io
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import connect

EXPORT_DIR = Path(__file__).parent.parent / "exports"
EXPORT_DIR.mkdir(exist_ok=True)


def generate_diary_pdf(project_id: int, diary_id: int) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    # Register Vietnamese font
    font_name = "DejaVuSans"
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf"),
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            pdfmetrics.registerFont(TTFont(font_name, fp))
            break
    else:
        raise RuntimeError(
            "No Vietnamese font found. Install DejaVu Sans "
            "(apt install fonts-dejavu-core) or place DejaVuSans.ttf "
            "next to export_pdf.py."
        )
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm

    c = connect()
    try:
        # Validate raw rows BEFORE dict(): a missing diary/project must yield
        # 404, not a TypeError from dict(None) leaking a 500 traceback.
        diary_row = c.execute("SELECT * FROM diaries WHERE id=?", (diary_id,)).fetchone()
        project_row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not diary_row or not project_row or diary_row["project_id"] != project_id:
            raise RuntimeError("PDF: diary/project mismatch or missing")
        diary = dict(diary_row)
        project = dict(project_row)
        labor = [dict(r) for r in c.execute(
            """SELECT lt.name, dl.count FROM diary_labor dl
               JOIN labor_types lt ON dl.labor_type_id=lt.id WHERE dl.diary_id=?""", (diary_id,))]
        machinery = [dict(r) for r in c.execute(
            """SELECT mt.name, dm.hours FROM diary_machinery dm
               JOIN machinery_types mt ON dm.machinery_type_id=mt.id WHERE dm.diary_id=?""", (diary_id,))]
        materials = [dict(r) for r in c.execute(
            "SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute(
                    "SELECT filename, caption, photo_kind, issue_id FROM diary_photos WHERE diary_id=?",
                    (diary_id,))]
        issues = [dict(r) for r in c.execute(
                    "SELECT * FROM diary_issues WHERE diary_id=? ORDER BY id",
                    (diary_id,))]
    finally:
        c.close()

    # Legacy single-issue fallback for rows that never migrated to the child
    # table (backward-compat read path).
    if not issues and diary.get("issues") and diary["issues"].strip():
        issues.append({
            "id": None, "description": diary["issues"],
            "cause": diary.get("cause") or "",
            "related_party": diary.get("related_party") or "",
            "suggestion": diary.get("issue_status") or "",
            "level": diary.get("issue_level") or "none",
            "workflow_status": diary.get("workflow_status") or "pending",
            "resolved_at": diary.get("resolved_at"),
        })

    # Resolve photo bytes from R2 (or local fallback) before building the PDF.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from app import r2
    local_uploads = Path(__file__).parent.parent / "uploads"
    for p in photos:
        data = r2.read_file(p["filename"])
        if data is None:
            local = local_uploads / p["filename"]
            if local.exists():
                data = local.read_bytes()
        p["_bytes"] = data

    filename = f"nhatky_{diary_id}_{diary['date']}.pdf"
    filepath = EXPORT_DIR / filename

    doc = SimpleDocTemplate(str(filepath), pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = font_name
    title_style = ParagraphStyle("Title", parent=normal, fontSize=16, alignment=1, spaceAfter=6)
    h2_style = ParagraphStyle("H2", parent=normal, fontSize=12, spaceBefore=8, spaceAfter=4)

    story = []
    story.append(Paragraph("NHẬT KÝ THI CÔNG", title_style))
    story.append(Paragraph(f"Dự án: {project['name']}", normal))
    story.append(Paragraph(f"Địa chỉ: {project.get('address', '')}", normal))
    story.append(Paragraph(f"Chủ đầu tư: {project.get('investor', '')} | TVGS: {project.get('supervisor', '')}", normal))
    story.append(Spacer(1, 0.3*cm))

    # Info table
    info_data = [
        ["Ngày", diary["date"], "Thời tiết", f"{diary.get('weather_temp', '')}°C — {diary.get('weather_desc', '')}"],
        ["Chỉ huy trưởng", diary.get("foreman", ""), "Tổng nhân lực", str(diary.get("total_workers", 0))],
    ]
    t = Table(info_data, colWidths=[3*cm, 5*cm, 3*cm, 5*cm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
        ("BACKGROUND", (2, 0), (2, -1), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.3*cm))

    # Labor
    story.append(Paragraph("1. NHÂN LỰC", h2_style))
    labor_data = [["Loại thợ", "Số lượng (người)"]]
    for l in labor:
        if l["count"] > 0:
            labor_data.append([l["name"], str(l["count"])])
    if len(labor_data) > 1:
        t = Table(labor_data, colWidths=[12*cm, 4*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTNAME",(0,0),(-1,-1),font_name), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Machinery
    story.append(Paragraph("2. MÁY MÓC THIẾT BỊ", h2_style))
    mach_data = [["Máy móc", "Số lượng"]]
    for m in machinery:
        if m["hours"] > 0:
            mach_data.append([m["name"], str(m["hours"])])
    if len(mach_data) > 1:
        t = Table(mach_data, colWidths=[12*cm, 4*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTNAME",(0,0),(-1,-1),font_name), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Materials
    if materials:
        story.append(Paragraph("3. VẬT TƯ NHẬP", h2_style))
        mat_data = [["Vật tư", "ĐVT", "Khối lượng"]]
        for m in materials:
            mat_data.append([m["name"], m.get("unit", ""), str(m["quantity"])])
        t = Table(mat_data, colWidths=[8*cm, 3*cm, 5*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTNAME",(0,0),(-1,-1),font_name), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Tasks
    if tasks:
        story.append(Paragraph("4. CÔNG VIỆC THỰC HIỆN", h2_style))
        task_data = [["Công việc", "KL", "ĐVT", "% lũy kế"]]
        for tsk in tasks:
            task_data.append([tsk["task_name"], str(tsk.get("quantity", "")), tsk.get("unit", ""), str(tsk.get("cumulative_pct", ""))])
        t = Table(task_data, colWidths=[8*cm, 3*cm, 3*cm, 3*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTNAME",(0,0),(-1,-1),font_name), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Notes
    if diary.get("notes"):
        story.append(Paragraph("5. GHI CHÚ", h2_style))
        story.append(Paragraph(diary["notes"], normal))

    # Issues (one-to-many child table, legacy fallback synthesised above).
    level_map = {"low": "🟢 Nhẹ", "medium": "🟡 Trung bình", "high": "🔴 Nghiêm trọng"}
    if issues:
        story.append(Paragraph("6. VƯỚNG MẮC & KIẾN NGHỊ", h2_style))
        for idx, issue in enumerate(issues, 1):
            story.append(Paragraph(
                f"<b>Issue #{idx}</b> — Mức độ: "
                f"{level_map.get(issue.get('level', ''), '')} | "
                f"Trạng thái: {issue.get('workflow_status', '')}", normal))
            story.append(Paragraph(f"Vướng mắc: {issue.get('description', '')}", normal))
            if issue.get("cause"):
                story.append(Paragraph(f"Nguyên nhân: {issue['cause']}", normal))
            if issue.get("related_party"):
                story.append(Paragraph(f"Liên quan: {issue['related_party']}", normal))
            if issue.get("suggestion"):
                story.append(Paragraph(f"Kiến nghị/Trạng thái: {issue['suggestion']}", normal))
            if issue.get("resolved_at"):
                story.append(Paragraph(f"Ngày giải quyết: {issue['resolved_at']}", normal))
            issue_photos = [p for p in photos
                            if p.get("issue_id") == issue.get("id")
                            or (issue.get("id") is None and p.get("photo_kind") == "issue")]
            for p in issue_photos:
                data = p.get("_bytes")
                if not data:
                    continue
                try:
                    img = Image(io.BytesIO(data), width=8*cm, height=6*cm)
                    story.append(img)
                    if p.get("caption"):
                        story.append(Paragraph(p["caption"], normal))
                    story.append(Spacer(1, 0.2*cm))
                except Exception:
                    pass

    # Photos — work/site photos only; per-issue photos already rendered in §6.
    work_photos = [p for p in photos
                   if not p.get("issue_id") and p.get("photo_kind") != "issue"]
    if work_photos:
        story.append(Paragraph("7. ẢNH HIỆN TRƯỜNG", h2_style))
        for p in work_photos:
            data = p.get("_bytes")
            if not data:
                continue
            try:
                img = Image(io.BytesIO(data), width=8*cm, height=6*cm)
                story.append(img)
                if p.get("caption"):
                    story.append(Paragraph(p["caption"], normal))
                story.append(Spacer(1, 0.2*cm))
            except Exception:
                pass

    # Signatures
    story.append(Spacer(1, 1*cm))
    sig_data = [
        ["Chỉ huy trưởng", "TVGS", "Chủ đầu tư"],
        ["", "", ""],
        ["(Ký, ghi rõ họ tên)", "(Ký, ghi rõ họ tên)", "(Ký, ghi rõ họ tên)"],
    ]
    t = Table(sig_data, colWidths=[5.3*cm, 5.3*cm, 5.3*cm])
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, 1), 40),
    ]))
    story.append(t)

    doc.build(story)
    return str(filepath)
