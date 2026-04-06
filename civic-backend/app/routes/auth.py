"""
Authentication Routes
=====================
Endpoints for phone OTP, Google OAuth, Aadhaar KYC, token management, and profile.

All login/verify endpoints return ``TokenResponse`` (access_token + refresh_token + user).
Access tokens expire in 15 minutes. Use ``POST /auth/refresh`` to rotate tokens.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.core.security import create_access_token
from app.database import get_db
from app.models.otp import OTP
from app.models.user import User
from app.schemas.auth import (
    AadharSendOTPRequest,
    AadharVerifyRequest,
    AuthProvidersResponse,
    ChangePhoneSendOTPRequest,
    ChangePhoneVerifyRequest,
    GoogleLoginRequest,
    RefreshTokenRequest,
    SendOTPRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
    VerifyOTPRequest,
)
from app.services.auth_service import (
    create_refresh_token,
    generate_otp,
    revoke_all_user_tokens,
    revoke_refresh_token,
    validate_refresh_token,
)
from app.services.sms_service import send_otp
from app.services.storage import upload_image

logger = get_logger("auth")

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/auth", tags=["Auth"])

OTP_EXPIRY_MINUTES = 10
OTP_COOLDOWN_SECONDS = 60


# ── Phone OTP ─────────────────────────────────────────────────────────────────


@router.post("/send-otp", status_code=status.HTTP_200_OK)
@limiter.limit("5/minute")
async def send_otp_route(request: Request, body: SendOTPRequest, db: Session = Depends(get_db)):
    """Send a 6-digit OTP to the given phone number via SMS.

    Rate-limited to **1 OTP per 60 seconds** per phone number.
    Any previously unused OTPs for this phone are invalidated.

    Returns:
        ``{"message": "OTP sent successfully"}``
        In DEV_MODE also returns ``{"dev_otp": "123456"}`` for testing.

    Raises:
        429: OTP requested too soon (wait 60 seconds).
        503: SMS delivery failed.
    """
    try:
        recent = (
            db.query(OTP)
            .filter(
                OTP.phone == body.phone,
                OTP.created_at > datetime.utcnow() - timedelta(seconds=OTP_COOLDOWN_SECONDS),
            )
            .first()
        )
        if recent:
            logger.warning(f"OTP rate limit hit for phone {body.phone}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait {OTP_COOLDOWN_SECONDS} seconds before requesting another OTP.",
            )

        # Invalidate any unused OTPs for this phone
        db.query(OTP).filter(
            OTP.phone == body.phone,
            OTP.is_used == False,
            OTP.expires_at > datetime.utcnow(),
        ).update({"is_used": True})
        db.commit()

        code = generate_otp()
        otp = OTP(
            phone=body.phone,
            code=code,
            expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES),
        )
        db.add(otp)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Database error while generating OTP for {body.phone}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate OTP. Please try again.",
        )

    sent = await send_otp(body.phone, code)
    if not sent:
        logger.error(f"SMS delivery failed for phone {body.phone}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to send OTP via SMS. Please try again.")

    logger.info(f"OTP generated and sent for phone {body.phone}")
    if settings.DEV_MODE:
        return {"message": "OTP sent successfully", "dev_otp": code}
    return {"message": "OTP sent successfully"}


@router.post("/verify-otp", response_model=TokenResponse)
@limiter.limit("10/minute")
def verify_otp_route(request: Request, body: VerifyOTPRequest, db: Session = Depends(get_db)):
    """Verify a phone OTP and return JWT tokens.

    Creates a new user account on first successful verification.
    Subsequent logins return the existing account.

    Returns:
        ``TokenResponse`` with access_token, refresh_token, and user profile.

    Raises:
        400: Invalid or expired OTP.
        403: Account is deactivated.
    """
    try:
        otp = (
            db.query(OTP)
            .filter(
                OTP.phone == body.phone,
                OTP.code == body.code,
                OTP.is_used == False,
                OTP.expires_at > datetime.utcnow(),
            )
            .first()
        )
        if not otp:
            logger.warning(f"Invalid or expired OTP attempt for phone {body.phone}")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")

        # Check if existing user is active BEFORE consuming the OTP so it can be reused if forbidden
        existing_user = db.query(User).filter(User.phone == body.phone).first()
        if existing_user and not existing_user.is_active:
            logger.warning(f"Login attempt on deactivated account (phone={body.phone})")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

        otp.is_used = True
        db.commit()

        user = existing_user
        if not user:
            user = User(phone=body.phone)
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"New user account created for phone {body.phone} (user_id={user.id})")

        access_token = create_access_token({"sub": str(user.id)})
        refresh_token = create_refresh_token(str(user.id), db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during OTP verification for {body.phone}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Verification failed. Please try again.",
        )

    logger.info(f"User {user.id} authenticated via phone OTP")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


# ── Google OAuth ──────────────────────────────────────────────────────────────


@router.post("/google", response_model=TokenResponse)
@limiter.limit("10/minute")
def google_login(request: Request, body: GoogleLoginRequest, db: Session = Depends(get_db)):
    """Authenticate via Google Sign-In.

    Verifies the Google ID token and returns JWT tokens.
    Account linking logic:
    1. If a user with this ``google_id`` exists → log them in.
    2. If no match but the email matches an existing account → link Google to that account.
    3. If no match at all → create a new user.

    Returns:
        ``TokenResponse`` with access_token, refresh_token, and user profile.

    Raises:
        401: Invalid Google token.
        403: Account is deactivated.
    """
    from app.services.google_auth import verify_google_id_token

    try:
        google_info = verify_google_id_token(body.id_token)
    except ValueError as e:
        logger.warning(f"Google token verification failed: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    google_id = google_info["google_id"]
    email = google_info.get("email")

    try:
        from sqlalchemy.exc import IntegrityError as SQLIntegrityError

        # 1. Find by google_id
        user = db.query(User).filter(User.google_id == google_id).first()

        # 2. Try to link to existing account by email
        if not user and email:
            user = db.query(User).filter(User.email == email).first()
            if user:
                user.google_id = google_id
                db.commit()
                logger.info(f"Linked Google account to existing user {user.id} via email {email}")

        # 3. Create new user (guard against concurrent duplicate inserts)
        if not user:
            try:
                user = User(
                    google_id=google_id,
                    email=email,
                    name=google_info.get("name"),
                )
                db.add(user)
                db.commit()
                db.refresh(user)
                logger.info(f"New user account created via Google (user_id={user.id})")
            except SQLIntegrityError:
                db.rollback()
                # Concurrent request already created the user — fetch it
                user = db.query(User).filter(User.google_id == google_id).first()
                if not user:
                    raise

        if not user.is_active:
            logger.warning(f"Google login attempt on deactivated account {user.id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

        access_token = create_access_token({"sub": str(user.id)})
        refresh_token = create_refresh_token(str(user.id), db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during Google authentication: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google authentication failed. Please try again.",
        )

    logger.info(f"User {user.id} authenticated via Google")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


# ── Aadhaar OTP ───────────────────────────────────────────────────────────────


@router.post("/aadhar/send-otp", status_code=status.HTTP_200_OK)
@limiter.limit("5/minute")
async def aadhar_send_otp(request: Request, body: AadharSendOTPRequest, db: Session = Depends(get_db)):
    """Request an OTP to the mobile number linked with the given Aadhaar number.

    Uses the configured KYC provider (Surepass or IDfy) to send the OTP.
    Returns a ``txn_id`` (also called ``client_id``) required for verification.

    Returns:
        ``{"client_id": "...", "txn_id": "...", "message": "OTP sent ..."}``
        In DEV_MODE also returns ``{"dev_otp": "123456"}``.

    Raises:
        400: Invalid Aadhaar number format.
        503: KYC provider failed or is unreachable.
    """
    from app.services.aadhar_service import send_aadhar_otp, validate_aadhar_format

    if not validate_aadhar_format(body.aadhaar_number):
        logger.warning(f"Invalid Aadhaar format submitted")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Aadhaar number format")

    try:
        result = await send_aadhar_otp(body.aadhaar_number, db)
    except RuntimeError as e:
        logger.error(f"Aadhaar OTP send failed: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error sending Aadhaar OTP: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send Aadhaar OTP. Please try again.",
        )

    logger.info(f"Aadhaar OTP sent successfully")
    return result


@router.post("/aadhar/verify", response_model=TokenResponse)
@limiter.limit("10/minute")
async def aadhar_verify_otp(request: Request, body: AadharVerifyRequest, db: Session = Depends(get_db)):
    """Verify the Aadhaar OTP and return JWT tokens.

    On success, creates or logs in the user. The Aadhaar number is
    hashed (SHA-256) before storage — it is **never stored in plaintext**.

    Returns:
        ``TokenResponse`` with access_token, refresh_token, and user profile.

    Raises:
        400: Invalid or expired Aadhaar OTP.
        403: Account is deactivated.
        503: KYC provider failed or is unreachable.
    """
    from app.services.aadhar_service import hash_aadhar, verify_aadhar_otp

    try:
        verified = await verify_aadhar_otp(body.aadhaar_number, body.otp, body.txn_id, db)
    except RuntimeError as e:
        logger.error(f"Aadhaar OTP verification provider error: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error verifying Aadhaar OTP: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Aadhaar verification failed. Please try again.",
        )

    if not verified:
        logger.warning(f"Invalid or expired Aadhaar OTP")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired Aadhaar OTP")

    try:
        aadhar_hash = hash_aadhar(body.aadhaar_number)

        user = db.query(User).filter(User.aadhar_hash == aadhar_hash).first()
        first_verification = False
        if not user:
            user = User(
                aadhar_hash=aadhar_hash,
                aadhar_verified=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            first_verification = True
            logger.info(f"New user account created via Aadhaar (user_id={user.id})")
        else:
            if not user.aadhar_verified:
                first_verification = True
                user.aadhar_verified = True
                db.commit()

        if not user.is_active:
            logger.warning(f"Aadhaar login attempt on deactivated account {user.id}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated")

        # One-time reward for completing Aadhaar verification
        if first_verification:
            try:
                from app.services.rewards_service import award_event
                award_event(db, user.id, "aadhar_verified", note="Aadhaar KYC completed")
            except Exception:
                pass

        access_token = create_access_token({"sub": str(user.id)})
        refresh_token = create_refresh_token(str(user.id), db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during Aadhaar authentication: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication failed. Please try again.",
        )

    logger.info(f"User {user.id} authenticated via Aadhaar")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.model_validate(user),
    )


# ── Token management ──────────────────────────────────────────────────────────


@router.post("/refresh", response_model=TokenResponse)
def refresh_token_route(body: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Exchange a refresh token for a new access + refresh token pair.

    Implements **token rotation**: the old refresh token is revoked immediately
    and a new pair is issued. This limits the damage if a refresh token is leaked.

    Database errors are handled with exponential backoff retry logic.

    Returns:
        ``TokenResponse`` with new access_token, refresh_token, and user profile.

    Raises:
        401: Invalid, expired, or already-revoked refresh token.
        503: Database temporarily unavailable (after retries exhausted).
    """
    from sqlalchemy.exc import OperationalError, DatabaseError

    try:
        # Token validation includes retry logic
        user_id = validate_refresh_token(body.refresh_token, db)
        if not user_id:
            logger.warning("Invalid or expired refresh token presented")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token"
            )

        # Query for user with error handling
        try:
            user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        except (OperationalError, DatabaseError) as e:
            logger.error(f"Database error fetching user {user_id}: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable. Please try again in a few moments."
            )

        if not user:
            logger.warning(f"Refresh token for non-existent or inactive user {user_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive"
            )

        # Rotate: revoke old token and create new one atomically so that a
        # mid-rotation DB failure cannot leave the user permanently locked out.
        try:
            revoke_refresh_token(body.refresh_token, db, commit=False)
            access_token = create_access_token({"sub": str(user.id)})
            new_refresh_token = create_refresh_token(str(user.id), db, commit=False)
            db.commit()
            logger.info(f"Refresh token rotated for user {user_id}")
        except (OperationalError, DatabaseError) as e:
            logger.error(f"Database error during token rotation for user {user_id}: {e}", exc_info=True)
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Token refresh temporarily unavailable. Please try again in a few moments."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during token refresh: {e}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token refresh failed. Please log in again.",
        )

    logger.info(f"Tokens successfully refreshed for user {user.id}")
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        user=UserResponse.model_validate(user),
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(body: RefreshTokenRequest, db: Session = Depends(get_db)):
    """Log out by revoking the provided refresh token.

    The access token will expire naturally (15 min). For immediate
    invalidation on all devices, call this endpoint per device or use
    ``DELETE /auth/account`` to deactivate the entire account.

    Returns:
        ``{"message": "Logged out successfully"}``
    """
    try:
        revoke_refresh_token(body.refresh_token, db)
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed. Please try again.",
        )

    logger.info("Refresh token revoked (logout)")
    return {"message": "Logged out successfully"}


# ── Profile & account management ─────────────────────────────────────────────


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Get the currently authenticated user's profile.

    Returns:
        ``UserResponse`` with the user's profile information.

    Raises:
        401: Not authenticated or token expired.
    """
    return UserResponse.model_validate(current_user)


@router.put("/profile", response_model=UserResponse)
def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's profile.

    All fields are optional — only provided (non-null) fields are updated.
    Phone and email uniqueness is enforced.

    Returns:
        Updated ``UserResponse``.

    Raises:
        401: Not authenticated.
        409: Phone or email already in use by another account.
    """
    try:
        if body.name is not None:
            current_user.name = body.name
        if body.ward is not None:
            current_user.ward = body.ward
        if body.ward_id is not None:
            current_user.ward_id = body.ward_id
        if body.taluka_id is not None:
            current_user.taluka_id = body.taluka_id
        if body.district_id is not None:
            current_user.district_id = body.district_id
        if body.language is not None:
            current_user.language = body.language
        if body.fcm_token is not None:
            current_user.fcm_token = body.fcm_token
        if body.email is not None:
            existing = db.query(User).filter(User.email == body.email, User.id != current_user.id).first()
            if existing:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use by another account")
            current_user.email = body.email
        if body.phone is not None:
            existing = db.query(User).filter(User.phone == body.phone, User.id != current_user.id).first()
            if existing:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone number already in use by another account")
            current_user.phone = body.phone
        if body.latitude is not None:
            current_user.latitude = body.latitude
        if body.longitude is not None:
            current_user.longitude = body.longitude

        db.commit()
        db.refresh(current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating profile for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Profile update failed. Please try again.",
        )

    logger.info(f"Profile updated for user {current_user.id}")
    return UserResponse.model_validate(current_user)


@router.get("/providers", response_model=AuthProvidersResponse)
def get_auth_providers(current_user: User = Depends(get_current_user)):
    """Check which authentication providers are linked to the current account.

    Useful for the frontend to show "Link Google", "Verify Aadhaar" buttons.

    Returns:
        ``AuthProvidersResponse`` showing linked phone, email, Google, and Aadhaar status.

    Raises:
        401: Not authenticated.
    """
    return AuthProvidersResponse(
        phone=current_user.phone,
        email=current_user.email,
        google_linked=current_user.google_id is not None,
        aadhar_verified=current_user.aadhar_verified,
    )


@router.delete("/account", status_code=status.HTTP_200_OK)
def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft-delete the current user's account.

    Deactivates the account and revokes all refresh tokens.
    Data is retained for audit purposes. Admins can reactivate via the admin panel.

    Returns:
        ``{"message": "Account deleted successfully"}``

    Raises:
        401: Not authenticated.
    """
    try:
        revoke_all_user_tokens(str(current_user.id), db)
        current_user.is_active = False
        db.commit()
    except Exception as e:
        logger.error(f"Error deactivating account {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Account deletion failed. Please try again.",
        )

    logger.info(f"Account {current_user.id} deactivated (soft-delete)")
    return {"message": "Account deleted successfully"}


@router.post("/profile/photo", response_model=UserResponse)
def upload_profile_photo(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload a profile photo for the current user.

    Accepts image files (JPEG, PNG). The file is uploaded to cloud storage
    and the URL is saved to the user's profile.

    **Roles**: any authenticated user.

    Returns:
        Updated ``UserResponse`` with the new profile_photo_url.

    Raises:
        400: Invalid file type.
        401: Not authenticated.
        413: File too large (max 5MB).
    """
    # Validate file type
    allowed_types = {"image/jpeg", "image/png", "image/jpg"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only JPEG and PNG images are allowed.",
        )

    # Validate file size (max 5MB)
    max_size = 5 * 1024 * 1024  # 5MB
    try:
        file_content = file.file.read()
        if len(file_content) > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File size must not exceed 5MB.",
            )
        file.file.seek(0)  # Reset file pointer
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating file for user {current_user.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to validate file.",
        )

    try:
        # Upload to storage service
        photo_url = upload_image(
            file_bytes=file_content,
            filename=file.filename,
            user_id=str(current_user.id)
        )
        
        # Update user's profile photo
        current_user.profile_photo_url = photo_url
        db.commit()
        db.refresh(current_user)
        
        logger.info(f"Profile photo uploaded for user {current_user.id}: {photo_url}")
        return UserResponse.model_validate(current_user)
    except Exception as e:
        logger.error(f"Error uploading profile photo for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload profile photo. Please try again.",
        )


# ── Phone number change ───────────────────────────────────────────────────────


@router.post("/phone/change/send-otp", status_code=status.HTTP_200_OK)
async def phone_change_send_otp(
    body: ChangePhoneSendOTPRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send an OTP to a new phone number to verify ownership before switching.

    The OTP is valid for 10 minutes. Rate-limited to 1 request per 60 seconds
    on the new phone number.

    **Roles**: any authenticated user.

    Returns:
        ``{"message": "OTP sent to new phone number"}``

    Raises:
        409: New phone number is already registered to another account.
        429: OTP cooldown — wait 60 seconds before retrying.
    """
    existing = db.query(User).filter(User.phone == body.new_phone, User.id != current_user.id).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone number already registered to another account")

    # Cooldown: check for a recent unexpired OTP on this new phone
    recent = (
        db.query(OTP)
        .filter(OTP.phone == body.new_phone, OTP.is_used == False)
        .order_by(OTP.created_at.desc())
        .first()
    )
    if recent:
        from datetime import timezone
        created = recent.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        if elapsed < OTP_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait {int(OTP_COOLDOWN_SECONDS - elapsed)}s before requesting another OTP",
            )

    code = generate_otp()
    expiry = datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES)
    otp = OTP(phone=body.new_phone, code=code, expires_at=expiry)
    db.add(otp)
    db.commit()

    sent = await send_otp(body.new_phone, code)
    if not sent:
        logger.error(f"SMS delivery failed for phone {body.new_phone}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to send OTP via SMS. Please try again.")
    logger.info(f"Phone change OTP sent to {body.new_phone} for user {current_user.id}")
    return {"message": "OTP sent to new phone number"}


@router.post("/phone/change/verify", response_model=UserResponse)
def phone_change_verify(
    body: ChangePhoneVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify the OTP and update the account's phone number.

    On success, the phone number is updated and all existing refresh tokens
    are revoked (forcing re-login on all devices for security).

    **Roles**: any authenticated user.

    Returns:
        Updated ``UserResponse`` with the new phone number.

    Raises:
        400: Invalid or expired OTP.
        409: Phone number already taken.
    """
    # Double-check uniqueness at verify time too
    existing = db.query(User).filter(User.phone == body.new_phone, User.id != current_user.id).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Phone number already registered to another account")

    from datetime import timezone
    now_utc = datetime.now(timezone.utc)
    otp_record = (
        db.query(OTP)
        .filter(
            OTP.phone == body.new_phone,
            OTP.code == body.code,
            OTP.is_used == False,
        )
        .order_by(OTP.created_at.desc())
        .first()
    )
    expires = otp_record.expires_at if otp_record else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if not otp_record or not expires or expires < now_utc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired OTP")

    try:
        otp_record.is_used = True
        current_user.phone = body.new_phone
        db.commit()
        db.refresh(current_user)

        # Revoke all tokens — user must re-login on all devices
        revoke_all_user_tokens(str(current_user.id), db)
    except Exception as e:
        logger.error(f"Error changing phone for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Phone change failed")

    logger.info(f"User {current_user.id} changed phone to {body.new_phone}")
    return UserResponse.model_validate(current_user)


# ── Data export (GDPR right-to-access) ───────────────────────────────────────


@router.get("/me/export")
@limiter.limit("3/hour")
def export_my_data(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Export all personal data for the current user (GDPR right-to-access).

    Returns a JSON object containing the user's profile, all reported issues,
    notification history, and reward transactions.

    Rate-limited to 3 requests per hour to prevent abuse.

    **Roles**: any authenticated user.
    """
    from app.models.issue import Issue
    from app.models.notification import Notification
    from app.models.reward import RewardTransaction, UserBadge

    try:
        issues = db.query(Issue).filter(
            Issue.reporter_id == current_user.id,
            Issue.is_deleted == False,
        ).order_by(Issue.created_at.desc()).all()

        notifications = db.query(Notification).filter(
            Notification.user_id == current_user.id
        ).order_by(Notification.created_at.desc()).limit(200).all()

        transactions = db.query(RewardTransaction).filter(
            RewardTransaction.user_id == current_user.id
        ).order_by(RewardTransaction.created_at.desc()).all()

        badges = db.query(UserBadge).filter(
            UserBadge.user_id == current_user.id
        ).order_by(UserBadge.earned_at.desc()).all()
    except Exception as e:
        logger.error(f"Error exporting data for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Data export failed. Please try again.",
        )

    logger.info(f"Data export requested by user {current_user.id}")
    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "profile": {
            "id": str(current_user.id),
            "phone": current_user.phone,
            "email": current_user.email,
            "name": current_user.name,
            "role": current_user.role,
            "ward": current_user.ward,
            "language": current_user.language,
            "aadhar_verified": current_user.aadhar_verified,
            "google_linked": current_user.google_id is not None,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        },
        "issues_reported": [
            {
                "id": str(i.id),
                "issue_type": i.issue_type,
                "severity": i.severity,
                "priority": i.priority,
                "status": i.status,
                "ward": i.ward,
                "address": i.address,
                "description": i.description,
                "upvote_count": i.upvote_count,
                "is_escalated": i.is_escalated,
                "citizen_rating": i.citizen_rating,
                "created_at": i.created_at.isoformat() if i.created_at else None,
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
            }
            for i in issues
        ],
        "notifications": [
            {
                "id": str(n.id),
                "title": n.title,
                "body": n.body,
                "type": n.type,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifications
        ],
        "reward_transactions": [
            {
                "points": t.points,
                "event_type": t.event_type,
                "note": t.note,
                "earned_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transactions
        ],
        "badges_earned": [
            {
                "badge_key": b.badge_key,
                "earned_at": b.earned_at.isoformat() if b.earned_at else None,
            }
            for b in badges
        ],
    }
