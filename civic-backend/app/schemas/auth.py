"""
Authentication Schemas
======================
Request/response models for all auth endpoints (OTP, Google, Aadhaar, tokens, profile).

Frontend Integration Notes:
- All token endpoints return ``TokenResponse`` containing access + refresh tokens.
- Access tokens expire in 15 minutes; use ``POST /auth/refresh`` to get a new pair.
- Phone numbers must include country code (e.g. ``+919876543210``).
"""

import re
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── OTP / Phone auth ──────────────────────────────────────────────────────────


class SendOTPRequest(BaseModel):
    """Request body for ``POST /auth/send-otp``.

    Sends a 6-digit OTP to the given phone number via SMS.
    Rate-limited to **1 request per 60 seconds** per phone number.

    Attributes:
        phone: Phone number with country code (10-15 digits, optional leading ``+``).
              Example: ``"+919876543210"``
    """

    phone: str = Field(..., examples=["+919876543210"], description="Phone number with country code")

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\+?[1-9]\d{9,14}$", v):
            raise ValueError("Invalid phone number. Must be 10-15 digits, optionally starting with +")
        return v


class VerifyOTPRequest(BaseModel):
    """Request body for ``POST /auth/verify-otp``.

    Verifies the OTP and returns JWT tokens. Creates a new user account
    on first successful verification.

    Attributes:
        phone: Same phone number used in ``send-otp``.
        code:  The 6-digit OTP received via SMS.
    """

    phone: str = Field(..., examples=["+919876543210"], description="Phone number used in send-otp")
    code: str = Field(..., examples=["123456"], description="6-digit OTP code")

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be exactly 6 digits")
        return v


# ── Google OAuth ──────────────────────────────────────────────────────────────


class GoogleLoginRequest(BaseModel):
    """Request body for ``POST /auth/google``.

    The client obtains an ``id_token`` from the Google Sign-In SDK and sends it here.
    The backend verifies the token with Google and returns JWT tokens.

    In **DEV_MODE**, ``id_token`` can be any arbitrary string (treated as a mock google_id).

    Attributes:
        id_token: Google ID token from the Google Sign-In SDK.
    """

    id_token: str = Field(..., description="Google ID token from client-side Google Sign-In SDK")


# ── Aadhaar auth ─────────────────────────────────────────────────────────────


class AadharSendOTPRequest(BaseModel):
    """Request body for ``POST /auth/aadhar/send-otp``.

    Sends an OTP to the mobile number linked with the given Aadhaar number
    via the configured KYC provider (Surepass or IDfy).

    Attributes:
        aadhaar_number: 12-digit Aadhaar number (must not start with 0 or 1).
    """

    aadhaar_number: str = Field(
        ...,
        examples=["234567890123"],
        description="12-digit Aadhaar number (no spaces, must not start with 0 or 1)",
    )

    @field_validator("aadhaar_number")
    @classmethod
    def validate_aadhaar(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[2-9]\d{11}$", v):
            raise ValueError("Aadhaar number must be 12 digits and must not start with 0 or 1")
        return v


class AadharVerifyRequest(BaseModel):
    """Request body for ``POST /auth/aadhar/verify``.

    Verifies the OTP sent to the Aadhaar-linked mobile number.
    On success, creates or logs in the user and returns JWT tokens.

    Attributes:
        aadhaar_number: Same Aadhaar number used in ``send-otp``.
        otp:            6-digit OTP received on the Aadhaar-linked mobile.
        txn_id:         Transaction ID returned by the ``send-otp`` endpoint
                        (also called ``client_id`` in the response).
    """

    aadhaar_number: str = Field(..., description="Same Aadhaar number used in send-otp")
    otp: str = Field(..., examples=["123456"], description="6-digit OTP from Aadhaar-linked mobile")
    txn_id: str = Field(..., description="Transaction ID from send-otp response (client_id)")

    @field_validator("aadhaar_number")
    @classmethod
    def validate_aadhaar(cls, v: str) -> str:
        v = v.strip().replace(" ", "")
        if not re.match(r"^[2-9]\d{11}$", v):
            raise ValueError("Aadhaar number must be 12 digits and must not start with 0 or 1")
        return v

    @field_validator("otp")
    @classmethod
    def validate_otp(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be exactly 6 digits")
        return v


# ── Shared responses ─────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    """User profile returned in auth responses and profile endpoints.

    Attributes:
        id:              Unique user UUID.
        phone:           Phone number (null if signed up via Google/Aadhaar only).
        email:           Email address (null if not provided).
        name:            Display name (null if not set yet).
        role:            One of ``"citizen"``, ``"worker"``, ``"admin"``.
        ward:            Ward/area the user belongs to (null if not set).
        language:        Preferred language — ``"en"`` | ``"hi"`` | ``"gu"``.
        is_active:       Whether the account is active.
        google_id:       Google account ID if Google auth is linked (null otherwise).
        aadhar_verified: Whether the user has completed Aadhaar verification.
        profile_photo_url: URL to the user's profile photo (null if not set).
        created_at:      Timestamp when the user account was created.
    """

    id: UUID
    phone: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    role: str
    ward: Optional[str] = None
    ward_id: Optional[UUID] = None
    taluka_id: Optional[UUID] = None
    district_id: Optional[UUID] = None
    department: Optional[str] = None
    language: str
    is_active: bool
    is_online: bool = False
    is_available: bool = True
    google_id: Optional[str] = None
    aadhar_verified: bool = False
    profile_photo_url: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """Returned by all login/verify/refresh endpoints.

    Attributes:
        access_token:  Short-lived JWT (15 min) for API authentication.
                       Send as ``Authorization: Bearer <access_token>`` header.
        refresh_token: Long-lived JWT (30 days) for obtaining new access tokens.
                       Use ``POST /auth/refresh`` to exchange it.
        token_type:    Always ``"bearer"``.
        user:          The authenticated user's profile.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshTokenRequest(BaseModel):
    """Request body for ``POST /auth/refresh`` and ``POST /auth/logout``.

    Attributes:
        refresh_token: The refresh token to exchange (refresh) or revoke (logout).
    """

    refresh_token: str = Field(..., description="Refresh token to exchange or revoke")


class UpdateProfileRequest(BaseModel):
    """Request body for ``PUT /auth/profile``.

    All fields are optional — only provided fields are updated.

    Attributes:
        name:         New display name.
        email:        Email address.
        language:     Preferred language — ``"en"`` (English), ``"hi"`` (Hindi), ``"gu"`` (Gujarati).
        ward_id:      Ward UUID.
        taluka_id:    Taluka UUID.
        district_id:  District UUID.
        fcm_token:    Firebase Cloud Messaging token for push notifications.
        phone:        Phone number (allows social-auth users to add phone later).
        ward:         Ward/area name (deprecated, use ward_id).
    """

    name: Optional[str] = Field(None, description="Display name")
    email: Optional[str] = Field(None, description="Email address")
    language: Optional[str] = Field(None, description="Preferred language: en, hi, or gu")
    ward_id: Optional[UUID] = Field(None, description="Ward UUID")
    taluka_id: Optional[UUID] = Field(None, description="Taluka UUID")
    district_id: Optional[UUID] = Field(None, description="District UUID")
    fcm_token: Optional[str] = Field(None, description="Firebase Cloud Messaging token for push notifications")
    phone: Optional[str] = Field(None, description="Phone number (for social-auth users to add phone later)")
    ward: Optional[str] = Field(None, description="Ward/area name (deprecated, use ward_id)")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Home latitude for citizen location services")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Home longitude for citizen location services")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("en", "hi", "gu"):
            raise ValueError("Language must be one of: en, hi, gu")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not re.match(r"^\+?[1-9]\d{9,14}$", v):
                raise ValueError("Invalid phone number")
        return v


class ChangePhoneSendOTPRequest(BaseModel):
    """Request body for ``POST /auth/phone/change/send-otp``.

    Sends an OTP to the new phone number to verify ownership before changing.
    Requires the user to be authenticated.

    Attributes:
        new_phone: The new phone number to switch to.
    """

    new_phone: str = Field(..., examples=["+919876543210"], description="New phone number with country code")

    @field_validator("new_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\+?[1-9]\d{9,14}$", v):
            raise ValueError("Invalid phone number. Must be 10-15 digits, optionally starting with +")
        return v


class ChangePhoneVerifyRequest(BaseModel):
    """Request body for ``POST /auth/phone/change/verify``.

    Verifies the OTP sent to the new phone and updates the account.

    Attributes:
        new_phone: Same new phone number used in send-otp.
        code:      The 6-digit OTP received on the new phone.
    """

    new_phone: str = Field(..., examples=["+919876543210"], description="New phone number used in send-otp")
    code: str = Field(..., examples=["123456"], description="6-digit OTP received on new phone")

    @field_validator("new_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^\+?[1-9]\d{9,14}$", v):
            raise ValueError("Invalid phone number")
        return v

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not v.isdigit() or len(v) != 6:
            raise ValueError("OTP must be exactly 6 digits")
        return v


class AuthProvidersResponse(BaseModel):
    """Response for ``GET /auth/providers``.

    Shows which authentication methods are linked to the current account.

    Attributes:
        phone:           Phone number if linked (null otherwise).
        email:           Email if linked (null otherwise).
        google_linked:   True if Google Sign-In is linked.
        aadhar_verified: True if Aadhaar verification is complete.
    """

    phone: Optional[str] = None
    email: Optional[str] = None
    google_linked: bool
    aadhar_verified: bool
