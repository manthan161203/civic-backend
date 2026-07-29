"""
SMS Service — OTP Delivery via MSG91
=====================================
Sends OTP messages to phone numbers using the MSG91 API.

With ``SMS_BACKEND="console"`` the OTP is logged instead of sent. That is the
only way an OTP is ever "delivered" without MSG91 — a missing or broken MSG91
configuration is reported as a failure, never as a success.
"""

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("sms")

MSG91_OTP_URL = "https://control.msg91.com/api/v5/otp"


async def send_otp(phone: str, code: str) -> bool:
    """Send a 6-digit OTP to the given phone number.

    Uses MSG91's OTP API, or logs the code when ``SMS_BACKEND="console"``.

    Args:
        phone: Phone number with country code (e.g. ``"+919876543210"``).
        code:  6-digit OTP code to send.

    Returns:
        True if the OTP was sent (or logged by the console backend), False on
        failure. The caller turns False into a 503 — see
        ``app/routes/auth.py``.
    """
    if settings.SMS_BACKEND == "console":
        logger.warning(f"[console] OTP for {phone}: {code}")
        return True

    # Previously this fell back to `return True` when the key was missing, so
    # /auth/send-otp answered "OTP sent successfully" while sending nothing —
    # locking every user out with no error surfaced anywhere.
    if not settings.MSG91_API_KEY or not settings.MSG91_TEMPLATE_ID:
        logger.error(
            "MSG91 is not configured (API key and/or template ID missing) — "
            "cannot send OTP to %s",
            phone,
        )
        return False

    # MSG91 expects number with country code, no + prefix
    normalized = phone.lstrip("+")

    headers = {
        "authkey": settings.MSG91_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }
    payload = {
        "template_id": settings.MSG91_TEMPLATE_ID,
        "mobile": normalized,
        "otp": code,
        "otp_expiry": 10,  # minutes
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(MSG91_OTP_URL, json=payload, headers=headers)
            data = resp.json()

            if resp.status_code == 200 and data.get("type") == "success":
                logger.info(f"OTP sent via MSG91 to {phone}")
                return True

            logger.error(f"MSG91 error for {phone}: status={resp.status_code}, response={data}")
            return False

    except httpx.TimeoutException:
        logger.error(f"MSG91 request timed out for phone {phone}")
        return False
    except httpx.ConnectError:
        logger.error(f"Could not connect to MSG91 API for phone {phone}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending OTP to {phone}: {e}", exc_info=True)
        return False
