"""
Storage Service — Image Upload and Deletion
============================================
Supports three backends, controlled by the ``STORAGE_BACKEND`` env variable:

- ``"local"``      — saves files to ``uploads/`` on disk (default, good for dev).
- ``"cloudinary"`` — uploads to Cloudinary with auto quality/format optimization.
- ``"supabase"``   — uploads to Supabase Storage (same project as your database).

The public interface (``upload_image``, ``delete_image``) is identical for all
three backends — routes and workers never need to know which is active.

To switch backends, just change ``STORAGE_BACKEND`` in your ``.env`` file and
restart the server.
"""

import uuid
from pathlib import Path
from typing import Optional

import cloudinary
import cloudinary.uploader

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("storage")

# Local uploads directory (used when STORAGE_BACKEND=local)
UPLOADS_DIR = Path(__file__).resolve().parents[2] / "uploads"

# Configure Cloudinary at module load (only used when STORAGE_BACKEND=cloudinary)
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
)


# ── Public interface ──────────────────────────────────────────────────────────


def upload_image(
    file_bytes: bytes,
    filename: str,
    user_id: str = "unknown",
    issue_id: str = "unknown",
) -> Optional[str]:
    """Upload an image and return its public URL.

    Backend is selected by ``settings.STORAGE_BACKEND``:
    - ``"local"``      → saves to ``uploads/{user_id}/{issue_id}/``
    - ``"cloudinary"`` → uploads to Cloudinary under ``civic/{user_id}/{issue_id}/``
    - ``"supabase"``   → uploads to Supabase Storage under ``{user_id}/{issue_id}/``

    Args:
        file_bytes: Raw bytes of the image file.
        filename:   Base filename (extension may be added if missing).
        user_id:    Uploader's user UUID (used for folder organisation).
        issue_id:   Related issue UUID (used for folder organisation).

    Returns:
        Public URL of the uploaded image, or None on failure.
    """
    backend = settings.STORAGE_BACKEND.lower()

    if backend == "cloudinary":
        return _upload_to_cloudinary(
            file_bytes, filename, folder=f"civic/{user_id}/{issue_id}"
        )
    if backend == "supabase":
        return _upload_to_supabase(file_bytes, filename, user_id, issue_id)

    # Default: local
    return _save_locally(file_bytes, filename, user_id, issue_id)


def delete_image(public_id: str) -> bool:
    """Delete an image by its public_id or URL.

    Backend is selected by ``settings.STORAGE_BACKEND``:
    - ``"local"``      → deletes the file at the given local URL path.
    - ``"cloudinary"`` → deletes from Cloudinary using the public_id.
    - ``"supabase"``   → deletes from Supabase Storage using the file path.

    Args:
        public_id: Cloudinary public_id, Supabase file path, or local URL.

    Returns:
        True on success, False on failure.
    """
    backend = settings.STORAGE_BACKEND.lower()

    if backend == "cloudinary":
        return _delete_from_cloudinary(public_id)
    if backend == "supabase":
        return _delete_from_supabase(public_id)

    return _delete_locally(public_id)


# ── Local backend ─────────────────────────────────────────────────────────────


def _save_locally(
    file_bytes: bytes, filename: str, user_id: str, issue_id: str
) -> Optional[str]:
    try:
        folder = UPLOADS_DIR / user_id / issue_id
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        if not any(safe_name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            safe_name += ".jpg"
        path = folder / safe_name
        path.write_bytes(file_bytes)
        url = f"/uploads/{user_id}/{issue_id}/{safe_name}"
        logger.info(f"[local] Image saved: {url}")
        return url
    except PermissionError as e:
        logger.error(f"[local] Permission denied saving {filename}: {e}")
        return None
    except OSError as e:
        logger.error(f"[local] OS error saving {filename}: {e}")
        return None
    except Exception as e:
        logger.error(f"[local] Unexpected error saving {filename}: {e}", exc_info=True)
        return None


def _delete_locally(url: str) -> bool:
    try:
        relative = url.lstrip("/")
        if relative.startswith("uploads/"):
            relative = relative[len("uploads/"):]
        path = UPLOADS_DIR / relative
        if path.exists():
            path.unlink()
            logger.info(f"[local] Deleted: {path}")
        else:
            logger.warning(f"[local] File not found for deletion: {path}")
        return True
    except Exception as e:
        logger.error(f"[local] Error deleting {url}: {e}", exc_info=True)
        return False


# ── Cloudinary backend ────────────────────────────────────────────────────────


def _upload_to_cloudinary(
    file_bytes: bytes, filename: str, folder: str
) -> Optional[str]:
    if not settings.CLOUDINARY_API_KEY:
        logger.warning("[cloudinary] CLOUDINARY_API_KEY not set — skipping upload")
        return None
    try:
        result = cloudinary.uploader.upload(
            file_bytes,
            folder=folder,
            public_id=filename,
            resource_type="image",
            transformation=[
                {"quality": "auto"},
                {"fetch_format": "auto"},
            ],
        )
        url = result.get("secure_url")
        logger.info(f"[cloudinary] Uploaded: {url}")
        return url
    except cloudinary.exceptions.Error as e:
        logger.error(f"[cloudinary] Upload error for {filename}: {e}")
        return None
    except Exception as e:
        logger.error(f"[cloudinary] Unexpected error uploading {filename}: {e}", exc_info=True)
        return None


def _delete_from_cloudinary(public_id: str) -> bool:
    try:
        result = cloudinary.uploader.destroy(public_id)
        if result.get("result") == "ok":
            logger.info(f"[cloudinary] Deleted: {public_id}")
            return True
        logger.warning(f"[cloudinary] Unexpected delete result for {public_id}: {result}")
        return False
    except Exception as e:
        logger.error(f"[cloudinary] Delete failed for {public_id}: {e}", exc_info=True)
        return False


# ── Supabase Storage backend ──────────────────────────────────────────────────


def _get_supabase_client():
    """Lazily create the Supabase client (only when backend=supabase)."""
    from supabase import create_client
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set when STORAGE_BACKEND=supabase"
        )
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


def _upload_to_supabase(
    file_bytes: bytes, filename: str, user_id: str, issue_id: str
) -> Optional[str]:
    try:
        client = _get_supabase_client()
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        if not any(safe_name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            safe_name += ".jpg"

        # Path inside the bucket: user_id/issue_id/filename.jpg
        file_path = f"{user_id}/{issue_id}/{safe_name}"
        bucket = settings.SUPABASE_STORAGE_BUCKET

        client.storage.from_(bucket).upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "false"},
        )

        # Build public URL
        url = f"{settings.SUPABASE_URL}/storage/v1/object/public/{bucket}/{file_path}"
        logger.info(f"[supabase] Uploaded: {url}")
        return url
    except RuntimeError as e:
        logger.error(f"[supabase] Config error: {e}")
        return None
    except Exception as e:
        logger.error(f"[supabase] Upload failed for {filename}: {e}", exc_info=True)
        return None


def _delete_from_supabase(file_path: str) -> bool:
    """Delete a file from Supabase Storage.

    Args:
        file_path: Either the full public URL or the path within the bucket
                   (e.g. ``user_id/issue_id/filename.jpg``).
    """
    try:
        client = _get_supabase_client()
        bucket = settings.SUPABASE_STORAGE_BUCKET

        # If a full URL was passed, extract just the path inside the bucket
        marker = f"/object/public/{bucket}/"
        if marker in file_path:
            file_path = file_path.split(marker, 1)[1]

        client.storage.from_(bucket).remove([file_path])
        logger.info(f"[supabase] Deleted: {file_path}")
        return True
    except RuntimeError as e:
        logger.error(f"[supabase] Config error: {e}")
        return False
    except Exception as e:
        logger.error(f"[supabase] Delete failed for {file_path}: {e}", exc_info=True)
        return False
