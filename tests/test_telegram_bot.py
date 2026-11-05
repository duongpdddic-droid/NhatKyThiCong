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