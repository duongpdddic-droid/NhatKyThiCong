"""Focused mocked tests for Telegram photo send: timeout, oversized image, normal send."""
import io
import os
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ["SECRET_KEY"] = "test-secret"

import app.telegram_bot as tb


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cursor(rows=None, fetchone=None):
    """Build a fake sqlite cursor.

    The production code iterates ``for r in c.execute(...)`` (sqlite3
    Connection.execute returns a cursor that is iterable), so __iter__ must
    yield the rows; fetchone / fetchall are also supported for callers that
    use them.
    """
    m = MagicMock()
    m.__iter__.return_value = iter(rows or [])
    m.fetchall.return_value = rows or []
    m.fetchone.return_value = fetchone
    return m


def test_send_photo_normal_send_succeeds():
    sent = {}

    async def fake_post(url, data=None, files=None):
        sent["url"] = url
        sent["data"] = data
        sent["files"] = files
        return MagicMock(status_code=200)

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        _run(tb.send_photo("chat-1", photo_bytes=b"small-bytes", caption="cap"))

    assert sent["url"].endswith("/sendPhoto")
    assert sent["data"]["chat_id"] == "chat-1"
    assert sent["data"]["caption"] == "cap"
    # _compress_photo passes small bytes through untouched; sendPhoto wraps
    # them in a (filename, bytes, content-type) tuple for multipart upload.
    photo_part = sent["files"]["photo"]
    assert photo_part[1] == b"small-bytes"


def test_send_photo_oversized_image_is_resized_under_telegram_limit():
    from PIL import Image

    # Noise (not a flat colour) so the JPEG actually exceeds 1 MiB at 4000x4000.
    import random
    random.seed(42)
    noise = Image.effect_noise((4000, 4000), 128)
    buf = io.BytesIO()
    noise.save(buf, format="JPEG", quality=92)
    original = buf.getvalue()
    assert len(original) > 1024 * 1024, f"fixture too small: {len(original)}"

    # Drive the compressor directly so the test asserts the *compression*
    # contract (<= 1 MiB, valid JPEG), not Telegram's sendPhoto limit.
    compressed = tb._compress_photo(original, max_bytes=1024 * 1024)
    assert len(compressed) <= 1024 * 1024
    Image.open(io.BytesIO(compressed))  # still a valid JPEG


def test_send_photo_timeout_does_not_raise_and_returns_error():
    import httpx

    call_count = {"n": 0}

    async def fake_post(url, data=None, files=None):
        call_count["n"] += 1
        raise httpx.WriteTimeout("timed out writing body")

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert call_count["n"] == 1
    assert result is not None
    assert result["ok"] is False
    assert "timeout" in result["error"].lower() or "write" in result["error"].lower()


def test_send_photo_api_error_returns_failure_not_exception():
    import httpx

    async def fake_post(url, data=None, files=None):
        raise httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=MagicMock(status_code=500)
        )

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert result["ok"] is False


def test_send_diary_report_continues_after_one_photo_timeout():
    """A slow photo must not block the remaining photos or raise 500."""
    sent_photos = []

    async def fake_send_message(chat_id, text):
        return None

    async def fake_send_photo(chat_id, photo_path="", caption="", photo_bytes=None):
        # send_photo now returns a dict, never raises.
        if caption == "B":
            return {"ok": False, "error": "Timeout Telegram: slow photo"}
        sent_photos.append(caption)
        return {"ok": True}

    photos = [
        {"filename": "a.png", "caption": "A"},
        {"filename": "b.png", "caption": "B"},
        {"filename": "c.png", "caption": "C"},
    ]
    chat_rows = [{"telegram_chat_id": "chat-1"}]
    diary_row = {
        "id": 1, "date": "2026-01-01", "weather_temp": "25",
        "weather_desc": "Clear", "total_workers": 10, "notes": "",
        "issues": "", "issue_level": "none", "issue_status": "",
    }
    project_row = {"id": 1, "name": "Test Project"}

    def fake_execute(sql, params=None):
        s = sql.upper() if isinstance(sql, str) else ""
        if "DIARY_PHOTOS" in s:
            return _cursor(rows=photos)
        if "RECIPIENTS" in s or "USER_PROJECTS" in s or "ROLE='ADMIN'" in s:
            return _cursor(rows=chat_rows)
        if "FROM DIARIES" in s:
            return _cursor(fetchone=diary_row)
        if "FROM PROJECTS" in s:
            return _cursor(fetchone=project_row)
        return _cursor()

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch.object(tb, "send_message", fake_send_message), \
         patch.object(tb, "send_photo", fake_send_photo), \
         patch("app.r2.read_file", return_value=b"img"), \
         patch.object(tb, "connect") as mock_connect:
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn

        result = _run(tb.send_diary_report(1, 1))

    assert "A" in sent_photos
    assert "C" in sent_photos
    # Any photo failure must surface as ok=False with a concise Vietnamese
    # error so the tg_error UI can show partial-send failures.
    assert result["ok"] is False
    assert result["photos_sent"] == 2
    assert result["photos_failed"] == 1
    assert "2" in result["error"] and "1" in result["error"]
    assert "photo_errors" in result


def test_compress_under_10mb_large_image_normalized():
    from PIL import Image
    import random

    random.seed(7)
    img = Image.effect_noise((2600, 2600), 128)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    original = buf.getvalue()
    # Under Telegram's 10 MB hard limit but still large/slow.
    assert len(original) < 10 * 1024 * 1024

    compressed = tb._compress_photo(original)
    assert len(compressed) <= 2 * 1024 * 1024
    out = Image.open(io.BytesIO(compressed))
    assert max(out.size) <= 1600


def test_send_message_api_failure_returns_failure():
    from unittest.mock import MagicMock

    async def fake_post(url, json=None):
        return MagicMock(status_code=200,
                         json=lambda: {"ok": False, "description": "Bad Request"})

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_message("chat-1", "hi"))

    assert result is not None
    assert result["ok"] is False
    assert "Bad Request" in result["error"]


def test_send_diary_report_reports_chat_failure():
    async def fake_send_message(chat_id, text):
        return {"ok": False, "error": "chat down"}

    async def fake_send_photo(chat_id, photo_path="", caption="", photo_bytes=None):
        return {"ok": True}

    photos = [{"filename": "a.png", "caption": "A"}]
    chat_rows = [{"telegram_chat_id": "chat-1"}]
    diary_row = {
        "id": 1, "date": "2026-01-01", "weather_temp": "25",
        "weather_desc": "Clear", "total_workers": 10, "notes": "",
        "issues": "", "issue_level": "none", "issue_status": "",
    }
    project_row = {"id": 1, "name": "Test Project"}

    def fake_execute(sql, params=None):
        s = sql.upper() if isinstance(sql, str) else ""
        if "DIARY_PHOTOS" in s:
            return _cursor(rows=photos)
        if "RECIPIENTS" in s or "USER_PROJECTS" in s or "ROLE='ADMIN'" in s:
            return _cursor(rows=chat_rows)
        if "FROM DIARIES" in s:
            return _cursor(fetchone=diary_row)
        if "FROM PROJECTS" in s:
            return _cursor(fetchone=project_row)
        return _cursor()

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch.object(tb, "send_message", fake_send_message), \
         patch.object(tb, "send_photo", fake_send_photo), \
         patch("app.r2.read_file", return_value=b"img"), \
         patch.object(tb, "connect") as mock_connect:
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn

        result = _run(tb.send_diary_report(1, 1))

    assert result["ok"] is False
    assert result["chat_ok"] == 0
    assert result["chat_fail"] == 1


def test_send_diary_report_continues_after_read_error():
    async def fake_send_message(chat_id, text):
        return {"ok": True}

    async def fake_send_photo(chat_id, photo_path="", caption="", photo_bytes=None):
        return {"ok": True}

    photos = [
        {"filename": "a.png", "caption": "A"},
        {"filename": "b.png", "caption": "B"},
        {"filename": "c.png", "caption": "C"},
    ]
    chat_rows = [{"telegram_chat_id": "chat-1"}]
    diary_row = {
        "id": 1, "date": "2026-01-01", "weather_temp": "25",
        "weather_desc": "Clear", "total_workers": 10, "notes": "",
        "issues": "", "issue_level": "none", "issue_status": "",
    }
    project_row = {"id": 1, "name": "Test Project"}

    def fake_execute(sql, params=None):
        s = sql.upper() if isinstance(sql, str) else ""
        if "DIARY_PHOTOS" in s:
            return _cursor(rows=photos)
        if "RECIPIENTS" in s or "USER_PROJECTS" in s or "ROLE='ADMIN'" in s:
            return _cursor(rows=chat_rows)
        if "FROM DIARIES" in s:
            return _cursor(fetchone=diary_row)
        if "FROM PROJECTS" in s:
            return _cursor(fetchone=project_row)
        return _cursor()

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch.object(tb, "send_message", fake_send_message), \
         patch.object(tb, "send_photo", fake_send_photo), \
         patch("app.r2.read_file", side_effect=[b"img", OSError("boom"), b"img"]), \
         patch.object(tb, "connect") as mock_connect:
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn

        result = _run(tb.send_diary_report(1, 1))

    assert result["photos_sent"] == 2
    assert result["photos_failed"] == 1
    assert any("boom" in e for e in result["photo_errors"])
    assert result["ok"] is False


def test_send_photo_json_ok_false_returns_failure():
    from unittest.mock import MagicMock

    async def fake_post(url, data=None, files=None):
        return MagicMock(status_code=200,
                         json=lambda: {"ok": False, "description": "PHOTO REJECTED"})

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert result is not None
    assert result["ok"] is False
    assert "PHOTO REJECTED" in result["error"]


def test_send_diary_report_escapes_html_and_truncates():
    captured = {}

    async def fake_send_message(chat_id, text):
        captured["text"] = text
        return {"ok": True}

    async def fake_send_photo(chat_id, photo_path="", caption="", photo_bytes=None):
        return {"ok": True}

    long_notes = "X" * 5000
    photos = []
    chat_rows = [{"telegram_chat_id": "chat-1"}]
    diary_row = {
        "id": 1, "date": "2026-01-01", "weather_temp": "25",
        "weather_desc": "<b>bad</b>", "total_workers": 10, "notes": long_notes,
        "issues": "<script>alert(1)</script>", "issue_level": "low",
        "issue_status": "ok",
    }
    project_row = {"id": 1, "name": "<script>P</script>"}

    def fake_execute(sql, params=None):
        s = sql.upper() if isinstance(sql, str) else ""
        if "DIARY_PHOTOS" in s:
            return _cursor(rows=photos)
        if "RECIPIENTS" in s or "USER_PROJECTS" in s or "ROLE='ADMIN'" in s:
            return _cursor(rows=chat_rows)
        if "FROM DIARIES" in s:
            return _cursor(fetchone=diary_row)
        if "FROM PROJECTS" in s:
            return _cursor(fetchone=project_row)
        return _cursor()

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch.object(tb, "send_message", fake_send_message), \
         patch.object(tb, "send_photo", fake_send_photo), \
         patch.object(tb, "connect") as mock_connect:
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn

        result = _run(tb.send_diary_report(1, 1))

    text = captured["text"]
    assert "&lt;script&gt;P&lt;/script&gt;" in text
    assert "&lt;b&gt;bad&lt;/b&gt;" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert len(text) <= 4096


def test_send_diary_report_failure_summary_includes_details():
    async def fake_send_message(chat_id, text):
        return {"ok": False, "error": "chat down"}

    async def fake_send_photo(chat_id, photo_path="", caption="", photo_bytes=None):
        return {"ok": False, "error": "PHOTO REJECTED"}

    photos = [{"filename": "a.png", "caption": "A"}]
    chat_rows = [{"telegram_chat_id": "chat-1"}]
    diary_row = {
        "id": 1, "date": "2026-01-01", "weather_temp": "25",
        "weather_desc": "Clear", "total_workers": 10, "notes": "",
        "issues": "", "issue_level": "none", "issue_status": "",
    }
    project_row = {"id": 1, "name": "Test Project"}

    def fake_execute(sql, params=None):
        s = sql.upper() if isinstance(sql, str) else ""
        if "DIARY_PHOTOS" in s:
            return _cursor(rows=photos)
        if "RECIPIENTS" in s or "USER_PROJECTS" in s or "ROLE='ADMIN'" in s:
            return _cursor(rows=chat_rows)
        if "FROM DIARIES" in s:
            return _cursor(fetchone=diary_row)
        if "FROM PROJECTS" in s:
            return _cursor(fetchone=project_row)
        return _cursor()

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch.object(tb, "send_message", fake_send_message), \
         patch.object(tb, "send_photo", fake_send_photo), \
         patch("app.r2.read_file", return_value=b"img"), \
         patch.object(tb, "connect") as mock_connect:
        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        conn.close = MagicMock()
        mock_connect.return_value = conn

        result = _run(tb.send_diary_report(1, 1))

    assert result["ok"] is False
    assert "details" in result
    assert isinstance(result["details"], str)
    assert "chat down" in result["details"]
    assert "PHOTO REJECTED" in result["details"]


def test_sanitize_error_redacts_bot_token():
    token = "123:SECRET"
    with patch.object(tb, "get_bot_token", return_value=token):
        redacted = tb.sanitize_error(f"failed for bot{token} endpoint")
    assert token not in redacted
    assert "[REDACTED]" in redacted


def test_send_message_http_400_includes_telegram_description():
    from unittest.mock import MagicMock

    async def fake_post(url, json=None):
        return MagicMock(
            status_code=400,
            json=lambda: {"ok": False, "description": "Bad Request: chat not found"},
        )

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_message("chat-1", "hi"))

    assert result is not None
    assert result["ok"] is False
    assert "HTTP 400" in result["error"]
    assert "chat not found" in result["error"]


def test_send_message_http_400_invalid_json_falls_back_safely():
    from unittest.mock import MagicMock

    async def fake_post(url, json=None):
        resp = MagicMock(status_code=400)
        resp.json.side_effect = ValueError("not json")
        return resp

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_message("chat-1", "hi"))

    assert result is not None
    assert result["ok"] is False
    assert "HTTP 400" in result["error"]


def test_send_message_http_400_redacts_token_in_description():
    from unittest.mock import MagicMock

    token = "123:SECRET"

    async def fake_post(url, json=None):
        return MagicMock(
            status_code=400,
            json=lambda: {"ok": False, "description": f"bad token {token}"},
        )

    with patch.object(tb, "get_bot_token", return_value=token), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_message("chat-1", "hi"))

    assert result is not None
    assert result["ok"] is False
    assert token not in result["error"]
    assert "[REDACTED]" in result["error"]


def test_send_photo_http_400_includes_telegram_description():
    from unittest.mock import MagicMock

    async def fake_post(url, data=None, files=None):
        return MagicMock(
            status_code=400,
            json=lambda: {"ok": False, "description": "Bad Request: wrong file format"},
        )

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert result is not None
    assert result["ok"] is False
    assert "HTTP 400" in result["error"]
    assert "wrong file format" in result["error"]


def test_send_photo_http_400_invalid_json_falls_back_safely():
    from unittest.mock import MagicMock

    async def fake_post(url, data=None, files=None):
        resp = MagicMock(status_code=400)
        resp.json.side_effect = ValueError("not json")
        return resp

    with patch.object(tb, "get_bot_token", return_value="123:TOKEN"), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert result is not None
    assert result["ok"] is False
    assert "HTTP 400" in result["error"]


def test_send_photo_http_400_redacts_token_in_description():
    from unittest.mock import MagicMock

    token = "123:SECRET"

    async def fake_post(url, data=None, files=None):
        return MagicMock(
            status_code=400,
            json=lambda: {"ok": False, "description": f"bad token {token}"},
        )

    with patch.object(tb, "get_bot_token", return_value=token), \
         patch("httpx.AsyncClient") as MockClient:
        client = MockClient.return_value.__aenter__.return_value
        client.post = fake_post
        result = _run(tb.send_photo("chat-1", photo_bytes=b"x", caption="c"))

    assert result is not None
    assert result["ok"] is False
    assert token not in result["error"]
    assert "[REDACTED]" in result["error"]