"""
Google OAuth Service — ID Token Verification
=============================================
Verifies Google ID tokens sent by the mobile/web client after Google Sign-In.

The mobile app uses the Google Sign-In SDK which provides an ``id_token``.
This backend verifies that token with Google's public keys — no redirect
flow or server-side OAuth is needed.

Required config:
    ``GOOGLE_CLIENT_ID`` — Your Google OAuth2 client ID (from Google Cloud Console).

GOOGLE_AUTH_BACKEND="console":
    Verification is bypassed and the ``id_token`` value is treated as a raw
    ``google_id`` string, so local development does not need a real Google
    project.

    This is a COMPLETE AUTHENTICATION BYPASS — any string logs you in as the
    corresponding account. It is rejected in production by
    ``validate_settings()``, and must never be enabled anywhere reachable from
    the internet.
"""

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("google_auth")


def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token and return the decoded user info.

    Verifies the token's signature against Google's public keys and checks the
    ``aud`` claim matches ``GOOGLE_CLIENT_ID``.

    With ``GOOGLE_AUTH_BACKEND="console"`` the token is instead treated as a
    mock ``google_id`` and no network call is made — see the module docstring
    for why that must never reach production.

    Args:
        id_token: Google ID token obtained from the client-side Google Sign-In SDK.

    Returns:
        Dict with keys:
        - ``google_id`` (str): Google account subject ID.
        - ``email`` (str | None): User's email address.
        - ``name`` (str | None): User's display name.
        - ``email_verified`` (bool): Whether the email is verified by Google.

    Raises:
        ValueError: If the token is invalid, expired, or the audience doesn't match.
    """
    if settings.GOOGLE_AUTH_BACKEND == "console":
        logger.warning(f"[console] Google auth bypass — mock google_id: {id_token}")
        return {
            "google_id": id_token,
            "email": f"{id_token}@mock.google.com",
            "name": "Google Test User",
            "email_verified": True,
        }

    if not settings.GOOGLE_CLIENT_ID:
        logger.error("GOOGLE_CLIENT_ID is not configured — cannot verify Google tokens")
        raise ValueError("GOOGLE_CLIENT_ID is not configured")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        idinfo = google_id_token.verify_oauth2_token(
            id_token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )

        user_info = {
            "google_id": idinfo["sub"],
            "email": idinfo.get("email"),
            "name": idinfo.get("name"),
            "email_verified": idinfo.get("email_verified", False),
        }
        logger.info(f"Google token verified for sub={idinfo['sub']}, email={idinfo.get('email')}")
        return user_info

    except ValueError as e:
        logger.warning(f"Google token verification failed (invalid token): {e}")
        raise ValueError(f"Invalid Google token: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during Google token verification: {e}", exc_info=True)
        raise ValueError(f"Google token verification error: {e}")