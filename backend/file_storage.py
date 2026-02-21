"""Supabase Storage client for persistent file storage.

Replaces local filesystem uploads with Supabase Storage buckets.
Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in environment.
"""

import logging
import httpx
from backend.config import settings

logger = logging.getLogger("rag.storage")

BUCKET = "legal-docs"
_TIMEOUT = 60.0


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
    }


def _storage_url(path: str) -> str:
    return f"{settings.SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}"


async def upload_file(path: str, file_bytes: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload a file to Supabase Storage.

    Args:
        path: Storage path within the bucket (e.g. "user_1/abc123.pdf")
        file_bytes: Raw file content
        content_type: MIME type

    Returns:
        The storage path on success

    Raises:
        RuntimeError on upload failure
    """
    url = _storage_url(path)
    headers = _headers()
    headers["Content-Type"] = content_type

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, content=file_bytes, headers=headers)
        if resp.status_code in (200, 201):
            logger.info(f"Uploaded {path} ({len(file_bytes)} bytes)")
            return path
        if resp.status_code == 400 and "Duplicate" in resp.text:
            resp = await client.put(url, content=file_bytes, headers=headers)
            if resp.status_code in (200, 201):
                logger.info(f"Overwritten {path} ({len(file_bytes)} bytes)")
                return path
        raise RuntimeError(f"Supabase Storage upload failed: {resp.status_code} {resp.text}")


async def download_file(path: str) -> bytes:
    """Download a file from Supabase Storage.

    Returns:
        Raw file bytes

    Raises:
        FileNotFoundError if not found, RuntimeError on other errors
    """
    url = _storage_url(path)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
        if resp.status_code == 200:
            return resp.content
        if resp.status_code == 404:
            raise FileNotFoundError(f"File not found in storage: {path}")
        raise RuntimeError(f"Supabase Storage download failed: {resp.status_code} {resp.text}")


async def delete_file(path: str) -> bool:
    """Delete a file from Supabase Storage.

    Returns:
        True if deleted, False if not found
    """
    url = f"{settings.SUPABASE_URL}/storage/v1/object/{BUCKET}"
    headers = _headers()
    headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.delete(url, headers=headers, json={"prefixes": [path]})
        if resp.status_code in (200, 201):
            logger.info(f"Deleted {path}")
            return True
        if resp.status_code == 404:
            logger.warning(f"File not found for deletion: {path}")
            return False
        logger.error(f"Supabase Storage delete failed: {resp.status_code} {resp.text}")
        return False


def storage_path_for_doc(user_id: int, filename: str) -> str:
    """Generate the storage path for a document file."""
    return f"user_{user_id}/{filename}"
