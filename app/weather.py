"""Weather API helper."""
import os
import httpx


async def get_weather(lat: float = 21.0, lon: float = 105.8) -> dict:
    api_key = os.getenv("OPENWEATHER_API_KEY", "")
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
