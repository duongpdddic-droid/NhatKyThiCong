# ── Weekly Report ───────────────────────────────────────────────────────────

from datetime import datetime, timedelta

@app.get("/projects/{project_id}/weekly-report", response_class=HTMLResponse)
def weekly_report_page(request: Request, project_id: int, user=Depends(require_role("admin")), week_start: str = None):
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
        
        c.execute("SELECT * FROM diaries WHERE project_id=? AND date BETWEEN ? AND ? ORDER BY date",
                  (project_id, week_start, week_end))
        diaries = [dict(r) for r in c.fetchall()]
        
        if diaries:
            diary_ids = [d["id"] for d in diaries]
            placeholders = ",".join("?" for _ in diary_ids)
            
            # Labor
            labor = c.execute(
                f"""SELECT lt.name, SUM(dl.count) as total_count
                    FROM diary_labor dl
                    JOIN labor_types lt ON dl.labor_type_id = lt.id
                    WHERE dl.diary_id IN ({placeholders})
                    GROUP BY lt.name ORDER BY total_count DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            
            # Machinery
            machinery = c.execute(
                f"""SELECT mt.name, SUM(dm.hours) as total_hours
                    FROM diary_machinery dm
                    JOIN machinery_types mt ON dm.machinery_type_id = mt.id
                    WHERE dm.diary_id IN ({placeholders})
                    GROUP BY mt.name ORDER BY total_hours DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            
            # Tasks
            tasks = c.execute(
                f"""SELECT task_name, SUM(quantity) as total_qty, unit, MAX(cumulative_pct) as max_pct
                    FROM diary_tasks
                    WHERE diary_id IN ({placeholders})
                    GROUP BY task_name, unit ORDER BY max_pct DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            
            # Acceptance
            acceptance = c.execute(
                f"""SELECT result, COUNT(*) as count, GROUP_CONCAT(DISTINCT inspector) as inspectors
                    FROM diary_acceptance
                    WHERE diary_id IN ({placeholders})
                    GROUP BY result ORDER BY count DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            
            # Issues
            issues = c.execute(
                f"""SELECT i.description, i.level, i.workflow_status, i.resolved_at,
                           d.date as diary_date
                    FROM diary_issues i
                    JOIN diaries d ON i.diary_id = d.id
                    WHERE i.diary_id IN ({placeholders})
                    AND i.workflow_status != 'resolved'
                    ORDER BY d.date DESC""",
                [d["id"] for d in diaries]
            ).fetchall()
            
            # Weather
            week_end_dt = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
            weather = c.execute(
                """SELECT session, AVG(temp) as avg_temp, GROUP_CONCAT(description) as descriptions
                   FROM project_weather_log
                   WHERE project_id = ? AND log_date BETWEEN ? AND ?
                   GROUP BY session""",
                (project_id, week_start, week_end_dt)
            ).fetchall()
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
async def weekly_report_send(request: Request, project_id: int, user=Depends(require_role("admin")), week_start: str = Form(...)):
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
        c.execute(
            """SELECT telegram_chat_id FROM recipients WHERE project_id=? AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' AND frequency IN ('daily', 'weekly')""",
            (project_id,)
        )
        chat_ids = {r["telegram_chat_id"] for r in c.fetchall() if r["telegram_chat_id"]}
        
        # Also get assigned users' telegram
        c.execute(
            """SELECT u.telegram_chat_id FROM users u
               JOIN user_projects up ON u.id = up.user_id
               WHERE up.project_id=? AND u.telegram_chat_id IS NOT NULL AND u.telegram_chat_id != ''""",
            (project_id,)
        )
        for r in c.fetchall():
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
        c.execute("SELECT * FROM diaries WHERE project_id=? AND date BETWEEN ? AND ? ORDER BY date",
                  (project_id, week_start, week_end))
        diaries = [dict(r) for r in c.fetchall()]
        
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
    for chat_id in chat_ids:
        try:
            from app.telegram_bot import send_message
            await send_message(token, chat_id, report_text)
            sent += 1
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
    
    return {"ok": True, "sent": sent, "errors": errors, "message": f"Đã gửi báo cáo tuần cho {sent} người nhận"}


@app.get("/projects/{project_id}/weekly-report/pdf")
def weekly_report_pdf(request: Request, project_id: int, user=Depends(require_role("admin")), week_start: str = None):
    """Generate weekly report PDF."""
    if not week_start:
        today = datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(today, "%Y-%m-%d")
        week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    
    week_end = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    
    # For now, redirect to the HTML page with a print-to-PDF instruction
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/projects/{project_id}/weekly-report?week_start={week_start}", status_code=302)