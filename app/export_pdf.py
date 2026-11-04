"""PDF export for construction diary."""
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
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm

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
        materials = [dict(r) for r in c.execute(
            "SELECT * FROM diary_materials WHERE diary_id=?", (diary_id,))]
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM diary_tasks WHERE diary_id=?", (diary_id,))]
        photos = [dict(r) for r in c.execute(
            "SELECT filename FROM diary_photos WHERE diary_id=?", (diary_id,))]
    finally:
        c.close()

    filename = f"nhatky_{diary_id}_{diary['date']}.pdf"
    filepath = EXPORT_DIR / filename

    doc = SimpleDocTemplate(str(filepath), pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"], fontSize=14, spaceAfter=6)
    h2_style = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
    normal = styles["Normal"]

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
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Machinery
    story.append(Paragraph("2. MÁY MÓC THIẾT BỊ", h2_style))
    mach_data = [["Máy móc", "Số giờ"]]
    for m in machinery:
        if m["hours"] > 0:
            mach_data.append([m["name"], str(m["hours"])])
    if len(mach_data) > 1:
        t = Table(mach_data, colWidths=[12*cm, 4*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Materials
    if materials:
        story.append(Paragraph("3. VẬT TƯ NHẬP", h2_style))
        mat_data = [["Vật tư", "ĐVT", "Khối lượng"]]
        for m in materials:
            mat_data.append([m["name"], m.get("unit", ""), str(m["quantity"])])
        t = Table(mat_data, colWidths=[8*cm, 3*cm, 5*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Tasks
    if tasks:
        story.append(Paragraph("4. CÔNG VIỆC THỰC HIỆN", h2_style))
        task_data = [["Công việc", "KL", "ĐVT", "% lũy kế"]]
        for tsk in tasks:
            task_data.append([tsk["task_name"], str(tsk.get("quantity", "")), tsk.get("unit", ""), str(tsk.get("cumulative_pct", ""))])
        t = Table(task_data, colWidths=[8*cm, 3*cm, 3*cm, 3*cm])
        t.setStyle(TableStyle([("GRID", (0,0),(-1,-1),0.5,colors.grey), ("FONTSIZE",(0,0),(-1,-1),9)]))
        story.append(t)

    # Notes
    if diary.get("notes"):
        story.append(Paragraph("5. GHI CHÚ", h2_style))
        story.append(Paragraph(diary["notes"], normal))

    # Issues
    if diary.get("issues") and diary["issues"].strip():
        story.append(Paragraph("6. VƯỚNG MẮC & KIẾN NGHỊ", h2_style))
        level_map = {"low": "🟢 Nhẹ", "medium": "🟡 Trung bình", "high": "🔴 Nghiêm trọng"}
        story.append(Paragraph(f"Mức độ: {level_map.get(diary.get('issue_level', ''), '')}", normal))
        story.append(Paragraph(f"Vướng mắc: {diary['issues']}", normal))
        if diary.get("issue_status"):
            story.append(Paragraph(f"Kiến nghị/Trạng thái: {diary['issue_status']}", normal))

    # Photos
    if photos:
        story.append(Paragraph("7. ẢNH HIỆN TRƯỜNG", h2_style))
        upload_dir = Path(__file__).parent.parent / "uploads"
        for p in photos:
            img_path = upload_dir / p["filename"]
            if img_path.exists():
                try:
                    img = Image(str(img_path), width=8*cm, height=6*cm)
                    story.append(img)
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
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, 1), 40),
    ]))
    story.append(t)

    doc.build(story)
    return str(filepath)
