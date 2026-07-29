"""
One-Time Setup Route
====================
Bootstraps the first admin account when no admin exists in the system.

Security model:
- The endpoint is **permanently disabled** once any admin account exists —
  active or not. It used to require ``is_active == True``, which meant
  deactivating the only admin silently re-opened an unauthenticated endpoint
  that mints a new super-admin.
- Returns 403 if called again after the first admin is created.
- Should be called once right after the first deployment/migration.
- In production, ``SETUP_TOKEN`` must be set and sent as the ``X-Setup-Token``
  header. Otherwise the window between migrating and creating the first admin
  is an open door: whoever reaches it first becomes super-admin.

Frontend Integration:
    Check GET /setup/status first — if {"setup_required": false}, skip this screen.
"""

import re

import secrets as _secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import get_logger
from app.database import get_db
from app.models.user import User
from app.schemas.auth import UserResponse
from app.services.utils import check_resource_exists, get_user_by_phone

logger = get_logger("setup")

router = APIRouter(prefix="/setup", tags=["Setup"])


class AdminSetupRequest(BaseModel):
    """Request body for ``POST /setup/admin``.

    Attributes:
        phone:    Admin's phone number with country code.
        name:     Admin's display name.
        ward:     Optional ward/area assignment.
        language: Preferred language — ``"en"`` | ``"hi"`` | ``"gu"``. Defaults to ``"en"``.
    """

    phone: str = Field(..., examples=["+919876543210"], description="Phone number with country code")
    name: str = Field(..., min_length=2, description="Admin's display name")
    ward: str | None = Field(None, description="Ward/area (optional)")
    language: str = Field("en", description="Preferred language: en, hi, or gu")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\+?[1-9]\d{9,14}$", v):
            raise ValueError("Invalid phone number. Must be 10-15 digits, optionally starting with +")
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        if v not in ("en", "hi", "gu"):
            raise ValueError("Language must be one of: en, hi, gu")
        return v


class SetupStatusResponse(BaseModel):
    """Response for ``GET /setup/status``.

    Attributes:
        setup_required: ``true`` if no admin exists yet — show the setup screen.
                        ``false`` if an admin already exists — skip to login.
    """

    setup_required: bool


@router.get("/status", response_model=SetupStatusResponse)
def setup_status(db: Session = Depends(get_db)):
    """Check whether first-time admin setup is still required.

    Call this on app launch to decide whether to show the setup screen.

    Returns:
        ``{"setup_required": true}``  — no admin exists, show setup.
        ``{"setup_required": false}`` — admin exists, go to login.
    """
    try:
        # Existence, not activeness — see the module docstring.
        admin_exists = check_resource_exists(User, User.role == "admin", db)
    except Exception as e:
        logger.error(f"Error checking setup status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check setup status.",
        )

    return SetupStatusResponse(setup_required=not admin_exists)


def _check_setup_token(provided: str | None) -> None:
    """In production, require a matching ``X-Setup-Token`` header.

    The lock below only holds once an admin exists. Between running migrations
    and creating that admin, this endpoint is an unauthenticated way to mint a
    super-admin, so production additionally requires a shared secret.
    ``validate_settings()`` warns when ``SETUP_TOKEN`` is unset.

    Raises:
        HTTPException: 403 if the token is missing or wrong.
    """
    if not settings.is_production:
        return

    expected = settings.SETUP_TOKEN
    if not expected:
        logger.error("SETUP_TOKEN is not configured — refusing unauthenticated setup")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Setup is not available.",
        )

    # compare_digest: the comparison is against a secret, so it should not leak
    # the matching prefix length through timing.
    if not provided or not _secrets.compare_digest(provided, expected):
        logger.warning("POST /setup/admin called with a missing or invalid setup token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Setup is not available.",
        )


@router.post("/admin", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_first_admin(
    body: AdminSetupRequest,
    db: Session = Depends(get_db),
    x_setup_token: str | None = Header(
        None,
        alias="X-Setup-Token",
        description="Required in production; must match SETUP_TOKEN.",
    ),
):
    """Create the first admin account (one-time setup only).

    This endpoint is **permanently disabled** once any admin account exists.
    Subsequent calls return ``403 Setup already completed``.

    Flow:
        1. Call ``GET /setup/status`` — if ``setup_required: false``, skip.
        2. Call this endpoint with admin details.
        3. Log in via ``POST /auth/send-otp`` + ``POST /auth/verify-otp``.
        4. Use the returned JWT to access all admin endpoints.

    Returns:
        ``UserResponse`` of the newly created admin.

    Raises:
        403: An admin account already exists (setup is locked), or the
             ``X-Setup-Token`` header is missing/invalid in production.
        409: Phone number is already registered to another account.
        422: Validation error in request body.
    """
    _check_setup_token(x_setup_token)

    try:
        # Lock: if any admin exists — active or not — this endpoint is disabled
        # permanently. Checking is_active meant deactivating the sole admin
        # re-opened it.
        admin_exists = check_resource_exists(User, User.role == "admin", db)

        if admin_exists:
            logger.warning("POST /setup/admin called but admin already exists — blocked")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Setup already completed. An admin account already exists.",
            )

        # Check phone uniqueness
        existing_phone = get_user_by_phone(body.phone, db)
        if existing_phone:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Phone number is already registered.",
            )

        admin = User(
            phone=body.phone,
            name=body.name,
            ward=body.ward,
            language=body.language,
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating first admin: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create admin account. Please try again.",
        )

    logger.info(f"First admin account created: id={admin.id}, phone={admin.phone}")
    return UserResponse.model_validate(admin)