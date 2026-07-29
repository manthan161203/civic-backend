"""
Security — password hashing and JWT creation/verification
=========================================================
Provides the low-level helpers used by the auth routes and the deps layer:

- ``hash_password`` / ``verify_password`` — Argon2id via passlib.
- ``create_access_token(data)``  — signs a short-lived access JWT.
- ``decode_access_token(token)`` — verifies signature, expiry **and token type**.

Token type is not decoration
----------------------------
Access tokens and refresh tokens are signed with the same ``SECRET_KEY`` and the
same algorithm. The only thing distinguishing them is the ``type`` claim. Access
tokens previously carried no ``type`` at all and ``decode_access_token`` checked
none, so a refresh token presented as ``Authorization: Bearer <refresh>`` sailed
through ``get_current_user`` — granting full API access for the refresh token's
**30-day** lifetime instead of the access token's 60 minutes, and surviving
``POST /auth/logout`` entirely, because the access path never consults
``refresh_tokens.is_revoked``. That defeated the whole point of the DB-backed
revocation table. Both halves are enforced here now.
"""

from datetime import timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.logger import get_logger
from app.core.time import now_utc

logger = get_logger("security")

# Value of the ``type`` claim on tokens issued by :func:`create_access_token`.
# ``app.services.auth_service`` owns the matching ``"refresh"`` constant.
ACCESS_TOKEN_TYPE = "access"

# ── Password hashing ─────────────────────────────────────
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT tokens ───────────────────────────────────────────
def create_access_token(data: dict) -> str:
    """Sign a short-lived access token.

    Args:
        data: Claims to embed. Must include ``sub`` (the user's UUID string).

    Returns:
        Signed JWT carrying ``type="access"`` and an expiry
        ``ACCESS_TOKEN_EXPIRE_MINUTES`` from now.
    """
    payload = data.copy()
    payload.update(
        {
            "exp": now_utc() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": now_utc(),
            "type": ACCESS_TOKEN_TYPE,
        }
    )
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """Verify an access token's signature, expiry and type.

    Args:
        token: Raw JWT string from the ``Authorization`` header.

    Returns:
        The decoded claims, or ``None`` if the token is invalid, expired, or is
        not an access token (e.g. a refresh token being replayed here).
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None

    token_type = payload.get("type")
    if token_type != ACCESS_TOKEN_TYPE:
        # A correctly signed token of the wrong kind. Most likely a refresh
        # token sent to a resource endpoint; log it, because it is either a
        # client bug or someone probing.
        logger.warning(
            "Token rejected: expected type %r, got %r", ACCESS_TOKEN_TYPE, token_type
        )
        return None

    return payload
