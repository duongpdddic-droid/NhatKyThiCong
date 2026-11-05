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
        delete_file(fn)
        removed.append(fn)
    return removed