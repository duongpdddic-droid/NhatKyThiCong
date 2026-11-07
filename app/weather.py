"""Weather API helper."""
import os
import httpx
from datetime import date


def _upsert_sql():
    from app.db import IS_PG
    return ("INSERT INTO project_weather_log (project_id, log_date, session, temp, description, icon) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(project_id, log_date, session) DO UPDATE SET "
            "temp=excluded.temp, description=excluded.description, icon=excluded.icon") if IS_PG else (
            "INSERT OR REPLACE INTO project_weather_log "
            "(project_id, log_date, session, temp, description, icon) VALUES (?,?,?,?,?,?)")


async def get_weather(lat: float = 21.0, lon: float = 105.8, api_key: str | None = None) -> dict:
    api_key = api_key if api_key is not None else os.getenv("OPENWEATHER_API_KEY", "")
    if not api_key:
        return {"temp": None, "desc": "Chưa cấu hình API key", "icon": "🌤️"}
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=vi"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=10)
            data = r.json()
        return {
            "temp": data.get("main", {}).get("temp"),
            "desc": data.get("weather", [{}])[0].get("description", ""),
            "icon": data.get("weather", [{}])[0].get("icon", ""),
        }
    except Exception:
        return {"temp": None, "desc": "Lỗi kết nối", "icon": "🌤️"}


async def ensure_weather(c, project_id: int, log_date: str, lat: float, lon: float,
                         session: str) -> dict | None:
    """Return cached weather, fetching only today's missing session."""
    row = c.execute(
        "SELECT temp, description AS desc, icon FROM project_weather_log "
        "WHERE project_id=? AND log_date=? AND session=?",
        (project_id, log_date, session)).fetchone()
    if row:
        return dict(row)
    if log_date != date.today().isoformat():
        return None
    setting = c.execute("SELECT value FROM settings WHERE key='openweather_api_key'").fetchone()
    api_key = setting["value"] if setting and setting["value"] else None
    value = await get_weather(lat, lon, api_key) if api_key else await get_weather(lat, lon)
    # A missing key/network failure is useful feedback but is not a weather
    # observation and must not poison the cache for a later retry.
    if value.get("temp") is None:
        return value
    c.execute(_upsert_sql(),
        (project_id, log_date, session, value.get("temp"), value.get("desc", ""), value.get("icon", "")))
    c.commit()
    return value


def store_weather(c, project_id: int, log_date: str, session: str, value: dict) -> None:
    """Persist a manually submitted/previously fetched observation."""
    if value.get("temp") in (None, "") and not value.get("desc"):
        return
    c.execute(_upsert_sql(),
        (project_id, log_date, session,
         float(value["temp"]) if value.get("temp") not in (None, "") else None,
         value.get("desc", ""), value.get("icon", "")))
