"""
Auth Service — OTP Generation and Refresh Token Management
===========================================================
Provides cryptographically secure OTP generation and DB-backed JWT refresh
tokens with per-token revocation support.

Refresh Token Design:
- Tokens are JWTs signed with SECRET_KEY (30-day expiry).
- A SHA-256 hash of each token is stored in the ``refresh_tokens`` table.
- On logout, the hash is marked ``is_revoked=True`` — the token is dead even
  if the JWT signature is still valid.
- On account deletion, ALL tokens for the user are revoked atomically.
"""

import hashlib
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("auth_service")

REFRESH_TOKEN_EXPIRE_DAYS = 30


def generate_otp() -> str:
    """Generate a cryptographically random 6-digit numeric OTP.

    Uses ``secrets.choice`` (CSPRNG) instead of ``random`` for security.

    Returns:
        6-digit string, e.g. ``"483920"``.
    """
    return "".join(secrets.choice(string.digits) for _ in range(6))


def _hash_token(token: str) -> str:
    """SHA-256 hash a JWT string for safe DB storage.

    Storing the hash instead of the raw token means a DB leak does not
    expose active tokens.

    Args:
        token: Raw JWT string.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(user_id: str, db: Session, commit: bool = True) -> str:
    """Create a DB-tracked refresh JWT for the given user.

    Stores a SHA-256 hash of the token in the ``refresh_tokens`` table so it
    can be individually revoked on logout without invalidating other sessions.

    Args:
        user_id: User's UUID string.
        db:      SQLAlchemy database session.
        commit:  Whether to commit immediately. Pass False when the caller
                 will manage the transaction (e.g. atomic token rotation).

    Returns:
        Signed JWT refresh token string.

    Raises:
        Exception: If the token cannot be saved to the database.
    """
    from app.models.refresh_token import RefreshToken

    expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    token_hash = _hash_token(token)

    try:
        record = RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        db.add(record)
        if commit:
            db.commit()
            logger.info(f"Refresh token created for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to save refresh token for user {user_id}: {e}", exc_info=True)
        raise

    return token


def validate_refresh_token(token: str, db: Session) -> Optional[str]:
    """Validate a refresh token: check JWT signature AND DB revocation status.

    A token is valid only if:
    1. The JWT signature is valid and not expired.
    2. The token type claim is ``"refresh"``.
    3. A matching non-revoked, non-expired record exists in the DB.

    Implements retry logic for transient database errors with exponential backoff.

    Args:
        token: Raw JWT refresh token string.
        db:    SQLAlchemy database session.

    Returns:
        User ID string if valid, None otherwise.

    Raises:
        Exception: On database errors after all retries exhausted.
    """
    from app.models.refresh_token import RefreshToken
    from app.services.retry_service import execute_with_retry, STANDARD_RETRY

    # First, validate JWT signature (no DB required, no retry needed)
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            logger.warning("Token rejected: type claim is not 'refresh'")
            return None
    except JWTError as e:
        logger.warning(f"Refresh token JWT validation failed: {e}")
        return None

    token_hash = _hash_token(token)

    # Query DB with retry logic for transient errors
    def query_token_from_db():
        return (
            db.query(RefreshToken)
            .filter(
                RefreshToken.token_hash == token_hash,
                RefreshToken.is_revoked == False,
                RefreshToken.expires_at > datetime.utcnow(),
            )
            .with_for_update()  # row-level lock prevents concurrent refresh race
            .first()
        )

    try:
        record = execute_with_retry(query_token_from_db, config=STANDARD_RETRY)
    except Exception as e:
        logger.error(f"Failed to validate refresh token after retries: {e}", exc_info=True)
        raise  # Re-raise so the route handler can catch and return 500

    if not record:
        logger.warning("Refresh token not found in DB or already revoked")
        return None

    logger.info(f"Refresh token validated successfully for user {payload.get('sub')}")
    return payload.get("sub")


def revoke_refresh_token(token: str, db: Session, commit: bool = True) -> bool:
    """Revoke a single refresh token by marking it as revoked in the DB.

    Args:
        token:  Raw JWT refresh token to revoke.
        db:     SQLAlchemy database session.
        commit: Whether to commit immediately. Pass False when the caller
                will manage the transaction (e.g. atomic token rotation).

    Returns:
        True if the token was found and revoked, False if not found.
    """
    from app.models.refresh_token import RefreshToken

    token_hash = _hash_token(token)
    try:
        updated = db.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash,
        ).update({"is_revoked": True})
        if commit:
            db.commit()
        if updated:
            logger.info("Refresh token revoked successfully")
        else:
            logger.warning("Revoke attempted on non-existent token hash")
        return updated > 0
    except Exception as e:
        logger.error(f"Error revoking refresh token: {e}", exc_info=True)
        return False


def revoke_all_user_tokens(user_id: str, db: Session) -> None:
    """Revoke all active refresh tokens for a user.

    Used on account deletion or forced logout from all devices.

    Args:
        user_id: User's UUID string.
        db:      SQLAlchemy database session.
    """
    from app.models.refresh_token import RefreshToken

    try:
        count = db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.is_revoked == False,
        ).update({"is_revoked": True})
        db.commit()
        logger.info(f"Revoked {count} refresh token(s) for user {user_id}")
    except Exception as e:
        logger.error(f"Error revoking all tokens for user {user_id}: {e}", exc_info=True)
        raise