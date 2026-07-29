"""
Storage Service — Image Upload and Deletion
============================================
Supports two backends, controlled by the ``STORAGE_BACKEND`` env variable:

- ``"local"``      — saves files to ``uploads/`` on disk and serves them at
                     ``/uploads``. This is the default and is used in production
                     too; the directory is a persistent Docker volume.
- ``"cloudinary"`` — uploads to Cloudinary with auto quality/format optimization.

The public interface (``upload_image``, ``delete_image``) is identical for both
backends — routes and workers never need to know which is active.

To switch backends, just change ``STORAGE_BACKEND`` in your ``.env`` file and
restart the server.
"""

import uuid
from typing import Optional
from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader

from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.core.logger import get_logger

logger = get_logger("storage")

# Local uploads directory (used when STORAGE_BACKEND=local). Same path the
# ``/uploads`` static mount serves from — see app/main.py.
UPLOADS_DIR = settings.uploads_path

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

    # Default: local
    return _save_locally(file_bytes, filename, user_id, issue_id)


def upload_image_or_raise(
    file_bytes: bytes,
    filename: str,
    user_id: str = "unknown",
    issue_id: str = "unknown",
) -> str:
    """Upload an image, raising if it could not be stored.

    Prefer this over :func:`upload_image` in request handlers. Storage failures
    used to be handled inconsistently — one route failed the request, four
    dropped the photo and returned 200, and one wrote ``None`` over the user's
    existing photo. Silently discarding a photo is the worst of those: the
    caller gets a success response and no reason to retry, and the image is
    gone for good.

    Args:
        file_bytes: Raw bytes of the image file.
        filename:   Base filename (extension may be added if missing).
        user_id:    Uploader's user UUID (used for folder organisation).
        issue_id:   Related issue UUID (used for folder organisation).

    Returns:
        Public URL of the uploaded image.

    Raises:
        ExternalServiceError: 503, when the storage backend returned no URL.
    """
    url = upload_image(file_bytes, filename, user_id=user_id, issue_id=issue_id)
    if not url:
        raise ExternalServiceError(
            "Storage",
            "Image upload failed — the file was not stored",
            transient=True,
        )
    return url


def delete_image(public_id: str) -> bool:
    """Delete an image by its public_id or URL.

    Backend is selected by ``settings.STORAGE_BACKEND``:
    - ``"local"``      → deletes the file at the given local URL path.
    - ``"cloudinary"`` → deletes from Cloudinary using the public_id.

    Args:
        public_id: Cloudinary public_id or local URL.

    Returns:
        True on success, False on failure.
    """
    backend = settings.STORAGE_BACKEND.lower()

    if backend == "cloudinary":
        return _delete_from_cloudinary(public_id)

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
        # Absolute URL. A root-relative path only resolves for clients served
        # from the same origin as the API, which is not true of the mobile app.
        base = settings.PUBLIC_BASE_URL.rstrip("/")
        url = f"{base}/uploads/{user_id}/{issue_id}/{safe_name}"
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
    """Delete a locally stored image given its URL.

    Accepts both the absolute URLs written today and the root-relative ones
    written before ``PUBLIC_BASE_URL`` existed. ``urlparse`` is what makes that
    work: stripping leading slashes off ``http://host/uploads/a/b.jpg`` leaves
    the scheme in place, so the old code built ``<uploads>/http:/host/uploads/...``,
    found nothing there, and logged "file not found" while returning True — every
    deletion silently no-opped and the files leaked.
    """
    try:
        path_part = urlparse(url).path.lstrip("/")
        if path_part.startswith("uploads/"):
            path_part = path_part[len("uploads/"):]
        if not path_part:
            logger.warning(f"[local] Refusing to delete, no file path in URL: {url}")
            return False

        root = UPLOADS_DIR.resolve()
        path = (root / path_part).resolve()

        # The URL is read back out of the database, so anything able to write a
        # photo URL could otherwise walk out of the uploads directory and delete
        # arbitrary files as the container user.
        if not path.is_relative_to(root):
            logger.error(f"[local] Refusing to delete outside uploads dir: {url}")
            return False

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
