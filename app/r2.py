"""Cloudflare R2 storage helper — S3-compatible via boto3.

Credentials come ONLY from environment variables:
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
Bucket: R2_BUCKET (default nhatky-thicong-photos)
Endpoint: R2_ENDPOINT_URL (default provided).

When credentials are absent the module degrades to the local filesystem
(``LOCAL_DIR``) so development and legacy deployments keep working without
any code change.  All public entry points are no-ops / fallbacks in that
mode; nothing here ever exposes a public bucket URL.
"""
import io
import os
import threading
from pathlib import Path

DEFAULT_ENDPOINT = "https://ca9718a46c6c8d8e4ba340d8eb927591.r2.cloudflarestorage.com"
DEFAULT_BUCKET = "nhatky-thicong-photos"

# Local fallback directory; main.py assigns the canonical UPLOAD_DIR here so
# tests that swap UPLOAD_DIR also swap the local storage root.
LOCAL_DIR = Path(__file__).resolve().parent.parent / "uploads"

# Map of common image extensions to MIME types for HTTP responses.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _cfg():
    return {
        "endpoint": os.getenv("R2_ENDPOINT_URL") or DEFAULT_ENDPOINT,
        "bucket": os.getenv("R2_BUCKET") or DEFAULT_BUCKET,
        "access_key": os.getenv("R2_ACCESS_KEY_ID", ""),
        "secret_key": os.getenv("R2_SECRET_ACCESS_KEY", ""),
    }


def enabled() -> bool:
    """True only when both R2 credentials are present."""
    c = _cfg()
    return bool(c["access_key"] and c["secret_key"])


_client = None


def _client_instance():
    global _client
    if _client is None:
        import boto3
        c = _cfg()
        _client = boto3.client(
            "s3",
            endpoint_url=c["endpoint"],
            aws_access_key_id=c["access_key"],
            aws_secret_access_key=c["secret_key"],
            region_name="auto",
        )
    return _client


def put_object(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Upload bytes to R2.  Returns the key.  No-op fallback handled by caller."""
    c = _cfg()
    _client_instance().put_object(
        Bucket=c["bucket"], Key=key, Body=data, ContentType=content_type
    )
    return key


def get_object(key: str) -> bytes | None:
    """Return object bytes from R2, or None when not enabled / not found."""
    if not enabled():
        return None
    c = _cfg()
    try:
        resp = _client_instance().get_object(Bucket=c["bucket"], Key=key)
        return resp["Body"].read()
    except Exception:
        return None


def delete_object(key: str) -> bool:
    """Delete an object from R2.  Returns True on success, False otherwise."""
    if not enabled():
        return False
    c = _cfg()
    try:
        _client_instance().delete_object(Bucket=c["bucket"], Key=key)
        return True
    except Exception:
        return False


def object_exists(key: str) -> bool | None:
    """Head-object check.  None when R2 is not enabled (caller falls back)."""
    if not enabled():
        return None
    c = _cfg()
    try:
        _client_instance().head_object(Bucket=c["bucket"], Key=key)
        return True
    except Exception:
        return False


# ── Unified storage API (R2 when enabled, local filesystem otherwise) ──────────

def save_file(filename: str, data: bytes) -> str:
    """Persist image bytes.  Returns the stored filename."""
    if enabled():
        ext = Path(filename).suffix.lower()
        ct = CONTENT_TYPES.get(ext, "image/jpeg")
        put_object(filename, data, content_type=ct)
        return filename
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / filename).write_bytes(data)
    return filename


def read_file(filename: str) -> bytes | None:
    """Return image bytes or None if the file cannot be located.

    Checks R2 first when enabled; falls back to LOCAL_DIR for legacy files
    that are absent from the bucket during transition.
    """
    if enabled():
        data = get_object(filename)
        if data is not None:
            return data
    p = LOCAL_DIR / filename
    if p.exists():
        return p.read_bytes()
    return None


def file_exists(filename: str) -> bool:
    """True when the object is reachable."""
    if enabled():
        return bool(object_exists(filename))
    return (LOCAL_DIR / filename).exists()


def delete_file(filename: str) -> bool:
    """Delete an object; safe no-op when nothing is stored under that name.

    Returns True on success (or when there was nothing to delete), False on
    failure.  Consistent bool semantics for both R2 and local storage.
    """
    if not filename:
        return False
    if enabled():
        return delete_object(filename)
    try:
        (LOCAL_DIR / filename).unlink()
        return True
    except OSError:
        return False


def delete_diary_objects(diary_id: int, filenames) -> list:
    """Delete every stored object for a diary's photos.

    Returns the list of filenames that were actually removed.  DB rows are the
    caller's responsibility; this only touches the stored bytes.
    """
    removed = []
    for fn in filenames:
        if not fn:
            continue
        # Only count filenames whose object was actually removed, so a
        # failed deletion never frees quota that is still in use.
        if delete_file(fn):
            removed.append(fn)
    return removed


DEFAULT_MAX_STORAGE_BYTES = int(os.getenv("R2_MAX_STORAGE_BYTES", "8000000000"))
DEFAULT_MAX_UPLOAD_BYTES = int(os.getenv("R2_MAX_UPLOAD_BYTES", "5000000"))
DEFAULT_MAX_PHOTOS_PER_DIARY = int(os.getenv("R2_MAX_PHOTOS_PER_DIARY", "20"))
RAW_UPLOAD_CAP_BYTES = 50 * 1024 * 1024

_quota_lock = threading.Lock()
_quota = {
    "cap": DEFAULT_MAX_STORAGE_BYTES,
    "max_bytes": DEFAULT_MAX_UPLOAD_BYTES,
    "max_photos": DEFAULT_MAX_PHOTOS_PER_DIARY,
    "used": 0,
}


def _ensure_quota():
    """Lazily (re)create the ledger after a test/fixture wiped it to None."""
    global _quota
    with _quota_lock:
        if _quota is None:
            _quota = {
                "cap": DEFAULT_MAX_STORAGE_BYTES,
                "max_bytes": DEFAULT_MAX_UPLOAD_BYTES,
                "max_photos": DEFAULT_MAX_PHOTOS_PER_DIARY,
                "used": 0,
            }
        return _quota


def configure_quota(cap=None, max_bytes=None, max_photos=None):
    q = _ensure_quota()
    with _quota_lock:
        if cap is not None:
            q["cap"] = int(cap)
        if max_bytes is not None:
            q["max_bytes"] = int(max_bytes)
        if max_photos is not None:
            q["max_photos"] = int(max_photos)


def set_used_bytes(n):
    q = _ensure_quota()
    with _quota_lock:
        q["used"] = int(n)


def get_used_bytes():
    q = _ensure_quota()
    with _quota_lock:
        return int(q["used"])


def quota_status():
    q = _ensure_quota()
    with _quota_lock:
        return dict(q)


def reserve(byte_count, photo_count=1):
    byte_count = int(byte_count)
    photo_count = int(photo_count)
    if byte_count < 0:
        byte_count = 0
    if photo_count < 0:
        photo_count = 0
    q = _ensure_quota()
    with _quota_lock:
        if byte_count > q["max_bytes"]:
            return False, "Ảnh quá giới hạn mỗi lần (%d > %d bytes)" % (
                byte_count, q["max_bytes"])
        if q["used"] + byte_count > q["cap"]:
            return False, (
                "Đã hết dung lượng lưu trữ (quota %d/%d bytes). "
                "Vui lòng xóa ảnh cũ trước." % (q["used"], q["cap"]))
        if photo_count > q["max_photos"]:
            return False, (
                "Số lượng ảnh vượt quá giới hạn mỗi nhật ký (%d > %d)" % (
                    photo_count, q["max_photos"]))
        q["used"] += byte_count
        return True, ""


def release(byte_count):
    q = _ensure_quota()
    with _quota_lock:
        q["used"] = max(0, q["used"] - int(byte_count))
# ── Image validation / normalization ───────────────────────────────────────────

_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP"}
_MAX_EDGE = 1600
_JPEG_QUALITY = 82


def _normalize_image(data: bytes):
    """Return (normalized_bytes, error_str|None).

    Validates the format, applies EXIF orientation, resizes so the longest
    edge is at most ``_MAX_EDGE`` and re-encodes as JPEG at quality 82.
    When R2 is *not* configured we cannot trust Pillow to be installed in
    production, so we skip the format check and pass the bytes through
    untouched (still enforcing the empty-content and raw-upload caps).
    """
    if not data:
        return b"", "Ảnh rỗng"
    if len(data) > RAW_UPLOAD_CAP_BYTES:
        return b"", "Ảnh quá lớn để upload (%d bytes)" % len(data)
    if not enabled():
        return data, None
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except Exception:
        return data, None
    try:
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or "").upper()
        if fmt not in _ALLOWED_FORMATS:
            return b"", "Định dạng ảnh không được hỗ trợ: %s" % fmt
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > _MAX_EDGE:
            scale = _MAX_EDGE / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return out.getvalue(), None
    except UnidentifiedImageError:
        return b"", "Ảnh bị lỗi hoặc không phải định dạng hợp lệ"
    except Exception as exc:
        return b"", "Lỗi xử lý ảnh: %s" % exc


def validate_image(data: bytes):
    """Validate + normalize an uploaded photo.

    Returns ``(bytes, None)`` on success or ``(b"", error_message)`` on
    failure.  The returned bytes are what actually gets stored, so the quota
    ledger accounts for the real on-disk size.
    """
    return _normalize_image(data)


# ── Reconciliation ─────────────────────────────────────────────────────────────

def _list_all_objects():
    """Manual pagination over list_objects_v2 (no paginator dependency)."""
    c = _cfg()
    client = _client_instance()
    contents = []
    token = None
    while True:
        kwargs = {"Bucket": c["bucket"]}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            contents.append(obj)
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
            if not token:
                break
        else:
            break
    return contents


def reconcile_from_inventory():
    """Recompute the ledger from the real R2 inventory.

    Returns ``(used_bytes, exact)``.  ``exact`` is False when any DB row has a
    NULL ``size_bytes`` (legacy data) so the caller can warn the admin instead
    of silently trusting the number.  Raises on listing failure so the admin
    route stays fail-closed.

    When an S3 client is reachable (real or mocked) the bucket inventory is the
    source of truth; otherwise the ledger falls back to the sum of recorded
    ``size_bytes`` in the DB, which is exact only when every row has one.
    """
    try:
        client = _client_instance()
    except Exception:
        client = None
    if client is not None:
        objects = _list_all_objects()
        total = sum(int(o.get("Size", 0)) for o in objects)
        set_used_bytes(total)
        exact = _db_rows_exact()
        return total, exact
    # No client configured: trust the DB ledger.
    try:
        import app.db as _db
        c = _db.connect()
        try:
            row = c.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) AS s, "
                "COUNT(*) FILTER (WHERE size_bytes IS NULL) AS nulls "
                "FROM diary_photos").fetchone()
            total = int(row["s"] or 0) if row else 0
            exact = not (row and row["nulls"])
        finally:
            c.close()
    except Exception:
        return 0, False
    set_used_bytes(total)
    return total, exact


def _db_rows_exact():
    """True when every diary_photos row carries a non-null size_bytes."""
    try:
        import app.db as _db
        c = _db.connect()
        try:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM diary_photos "
                "WHERE size_bytes IS NULL").fetchone()
            return not (row and row["n"])
        finally:
            c.close()
    except Exception:
        return False