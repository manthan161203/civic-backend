"""
Aadhaar OTP Authentication Service
====================================
Authenticates users via Aadhaar-linked mobile OTP using third-party KYC providers.

Supported providers (set ``AADHAR_KYC_PROVIDER`` in ``.env``):
  - **surepass** — https://surepass.io (pay-per-use, no enterprise contract needed)
  - **idfy**      — https://idfy.com (enterprise, requires account-id)

Privacy note:
  Aadhaar numbers are **never stored in plaintext**. Only a SHA-256 hash is
  persisted in the database, making the stored value non-reversible.

Authentication flow:
  1. ``POST /auth/aadhar/send-otp``
       → Validate Aadhaar format
       → Call KYC provider → OTP sent to Aadhaar-linked mobile
       → Returns ``client_id`` / ``txn_id`` (required in step 2)

  2. ``POST /auth/aadhar/verify``
       → Submit OTP + ``client_id`` to KYC provider
       → On success: create/login user with hashed Aadhaar

Required ``.env`` keys:
  ``AADHAR_KYC_PROVIDER``   = surepass or idfy
  ``AADHAR_KYC_API_KEY``    = provider API token
  ``AADHAR_KYC_URL``        = provider base URL
  ``AADHAR_KYC_ACCOUNT_ID`` = (IDfy only) account identifier

AADHAAR_BACKEND="console":
  Fully mocked — no real API calls, OTP printed to console and returned in the
  response. Rejected in production by ``validate_settings()``.
"""

import hashlib
import re
import uuid as _uuid
from typing import Optional

from app.core.time import now_utc
from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("aadhar")

_AADHAR_PREFIX = "aadhar:"


# ── Helpers ───────────────────────────────────────────────────────────────────


def hash_aadhar(aadhaar_number: str) -> str:
    """Compute SHA-256 hash of an Aadhaar number for safe storage.

    The plaintext Aadhaar is never stored — only this hash is saved to the DB.

    Args:
        aadhaar_number: 12-digit Aadhaar number string.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(aadhaar_number.encode()).hexdigest()


def validate_aadhar_format(aadhaar_number: str) -> bool:
    """Validate the basic format of an Aadhaar number.

    Rules: exactly 12 digits, first digit must be 2-9 (UIDAI specification).

    Args:
        aadhaar_number: String to validate.

    Returns:
        True if valid, False otherwise.
    """
    return bool(re.match(r"^[2-9]\d{11}$", aadhaar_number))


def _masked(aadhaar_number: str) -> str:
    """Return a masked Aadhaar number for safe logging (shows only last 4 digits).

    Args:
        aadhaar_number: Full 12-digit Aadhaar number.

    Returns:
        Masked string like ``"XXXX-XXXX-1234"``.
    """
    return f"XXXX-XXXX-{aadhaar_number[-4:]}"


# ── DEV MODE mock ─────────────────────────────────────────────────────────────


async def _dev_send_otp(aadhaar_number: str, db) -> dict:
    """Mock Aadhaar OTP send for the console backend — stores OTP in DB, logs to console.

    Args:
        aadhaar_number: Aadhaar number to mock-send OTP for.
        db:             SQLAlchemy session.

    Returns:
        Dict with ``client_id``, ``txn_id``, ``message``, and ``dev_otp``.
    """
    from datetime import timedelta
    from app.models.otp import OTP
    from app.services.auth_service import generate_otp

    # Key on the hash, never the raw number: this value is written to
    # otps.phone, and the module contract is that plaintext Aadhaar is never
    # persisted. Verification derives the same key the same way.
    key = f"{_AADHAR_PREFIX}{hash_aadhar(aadhaar_number)}"
    client_id = str(_uuid.uuid4())

    # Invalidate any previous unused OTPs for this Aadhaar
    db.query(OTP).filter(OTP.phone == key, OTP.is_used == False).update({"is_used": True})
    db.commit()

    code = generate_otp()
    db.add(OTP(phone=key, code=code, expires_at=now_utc() + timedelta(minutes=10)))
    db.commit()

    logger.info(f"[console] Aadhaar OTP for {_masked(aadhaar_number)}: {code}")
    return {
        "client_id": client_id,
        "message": "OTP sent to Aadhaar-linked mobile (console mock)",
        "dev_otp": code,
        "txn_id": client_id,
    }


async def _dev_verify_otp(aadhaar_number: str, otp: str, db) -> Optional[dict]:
    """Mock Aadhaar OTP verification for the console backend — checks OTP from DB.

    Args:
        aadhaar_number: Aadhaar number being verified.
        otp:            OTP code to verify.
        db:             SQLAlchemy session.

    Returns:
        Mock demographic dict on success, None if OTP is invalid/expired.
    """
    from app.models.otp import OTP

    key = f"{_AADHAR_PREFIX}{hash_aadhar(aadhaar_number)}"
    record = db.query(OTP).filter(
        OTP.phone == key,
        OTP.code == otp,
        OTP.is_used == False,
        OTP.expires_at > now_utc(),
    ).first()

    if not record:
        logger.warning(f"[console] Invalid or expired Aadhaar OTP for {_masked(aadhaar_number)}")
        return None

    record.is_used = True
    db.commit()

    logger.info(f"[console] Aadhaar OTP verified for {_masked(aadhaar_number)}")
    return {
        "verified": True,
        "name": "Aadhaar Test User",
        "gender": "M",
        "dob": "01-01-1990",
        "state": "Gujarat",
        "zip": "380001",
    }


# ── Surepass provider ─────────────────────────────────────────────────────────
# Docs: https://docs.surepass.io/aadhaar-offline-kyc
# Base URL: https://kyc-api.surepass.io/api/v1


async def _surepass_send_otp(aadhaar_number: str) -> dict:
    """Request Aadhaar OTP via Surepass API.

    Calls ``POST /aadhaar-v2/generate-otp``.

    Args:
        aadhaar_number: 12-digit Aadhaar number.

    Returns:
        Dict with ``client_id``, ``txn_id``, and ``message``.

    Raises:
        RuntimeError: On provider timeout, connection error, or unexpected response.
        ValueError:   On invalid Aadhaar number as per provider, or bad API key.
    """
    import httpx

    url = f"{settings.AADHAR_KYC_URL.rstrip('/')}/aadhaar-v2/generate-otp"
    headers = {
        "Authorization": f"Bearer {settings.AADHAR_KYC_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json={"id_number": aadhaar_number}, headers=headers)
        except httpx.TimeoutException:
            logger.error("Surepass API timed out during OTP send")
            raise RuntimeError("Aadhaar KYC provider timed out. Please try again.")
        # HTTPError is the base class: it also covers UnsupportedProtocol and
        # InvalidURL, which a malformed AADHAR_KYC_URL raises. Catching only
        # ConnectError let those escape as an unhandled 500.
        except httpx.HTTPError as e:
            logger.error("Cannot reach Surepass API: %s", type(e).__name__)
            raise RuntimeError("Cannot reach Aadhaar KYC provider. Check network.")

    if resp.status_code == 401:
        logger.error("Surepass API rejected request: invalid API key")
        raise RuntimeError("Invalid Aadhaar KYC API key.")
    if resp.status_code == 422:
        raise ValueError("Invalid Aadhaar number as per KYC provider.")
    if not resp.is_success:
        logger.error(f"Surepass OTP error {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"KYC provider error: {resp.status_code}")

    data = resp.json()
    if not data.get("success"):
        msg = data.get("message", "KYC provider rejected the request")
        logger.warning(f"Surepass OTP send rejected: {msg}")
        raise ValueError(msg)

    client_id = data.get("data", {}).get("client_id")
    if not client_id:
        logger.error("Surepass response missing client_id")
        raise RuntimeError("KYC provider did not return a client_id")

    logger.info(f"Surepass OTP sent for {_masked(aadhaar_number)}, client_id={client_id}")
    return {"client_id": client_id, "txn_id": client_id, "message": "OTP sent to Aadhaar-linked mobile"}


async def _surepass_verify_otp(client_id: str, otp: str) -> Optional[dict]:
    """Verify Aadhaar OTP via Surepass API.

    Calls ``POST /aadhaar-v2/submit-otp``.

    Args:
        client_id: Transaction ID returned by ``_surepass_send_otp``.
        otp:       6-digit OTP from the Aadhaar-linked mobile.

    Returns:
        Demographic dict (verified, name, gender, dob, state, zip, photo_link)
        or None if OTP is wrong.

    Raises:
        RuntimeError: On provider timeout.
    """
    import httpx

    url = f"{settings.AADHAR_KYC_URL.rstrip('/')}/aadhaar-v2/submit-otp"
    headers = {
        "Authorization": f"Bearer {settings.AADHAR_KYC_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json={"client_id": client_id, "otp": otp}, headers=headers)
        except httpx.TimeoutException:
            logger.error("Surepass API timed out during OTP verification")
            raise RuntimeError("Aadhaar KYC provider timed out. Please try again.")
        except httpx.HTTPError as e:
            logger.error("Cannot reach Surepass API: %s", type(e).__name__)
            raise RuntimeError("Cannot reach Aadhaar KYC provider. Check network.")

    if not resp.is_success:
        logger.warning(f"Surepass OTP verify failed {resp.status_code}: {resp.text[:200]}")
        return None

    data = resp.json()
    if not data.get("success"):
        logger.warning("Surepass OTP verify: success=false")
        return None

    kyc = data.get("data", {})
    logger.info(f"Surepass OTP verified for client_id={client_id}")
    return {
        "verified": True,
        "name": kyc.get("full_name"),
        "gender": kyc.get("gender"),
        "dob": kyc.get("dob"),
        "state": kyc.get("address", {}).get("state"),
        "zip": kyc.get("zip"),
        "photo_link": kyc.get("photo_link"),
    }


# ── IDfy provider ─────────────────────────────────────────────────────────────
# Docs: https://idfy-hackathon.gitbook.io/idfy/aadhaar-api
# Base URL: https://eve.idfy.com/v3/tasks/sync


async def _idfy_send_otp(aadhaar_number: str) -> dict:
    """Request Aadhaar OTP via IDfy API.

    Calls ``POST /generate_aadhaar_otp``.

    Args:
        aadhaar_number: 12-digit Aadhaar number.

    Returns:
        Dict with ``client_id``, ``txn_id``, and ``message``.

    Raises:
        RuntimeError: On provider timeout or error response.
    """
    import httpx

    url = f"{settings.AADHAR_KYC_URL.rstrip('/')}/generate_aadhaar_otp"
    task_id = str(_uuid.uuid4())
    group_id = str(_uuid.uuid4())

    headers = {
        "api-key": settings.AADHAR_KYC_API_KEY,
        "account-id": settings.AADHAR_KYC_ACCOUNT_ID,
        "Content-Type": "application/json",
    }
    body = {
        "task_id": task_id,
        "group_id": group_id,
        "data": {"aadhaar_number": aadhaar_number},
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            logger.error("IDfy API timed out during OTP send")
            raise RuntimeError("IDfy KYC provider timed out.")
        except httpx.HTTPError as e:
            logger.error("Cannot reach IDfy API: %s", type(e).__name__)
            raise RuntimeError("Cannot reach Aadhaar KYC provider. Check network.")

    if not resp.is_success:
        logger.error(f"IDfy OTP error {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"KYC provider error: {resp.status_code}")

    result = resp.json().get("result", {})
    if not result.get("is_otp_sent"):
        raise RuntimeError("IDfy could not send OTP. Check Aadhaar number.")

    request_id = resp.json().get("request_id", task_id)
    logger.info(f"IDfy OTP sent for {_masked(aadhaar_number)}, request_id={request_id}")
    return {"client_id": request_id, "txn_id": request_id, "message": "OTP sent to Aadhaar-linked mobile"}


async def _idfy_verify_otp(aadhaar_number: str, otp: str, request_id: str) -> Optional[dict]:
    """Verify Aadhaar OTP via IDfy API.

    Calls ``POST /submit_aadhaar_otp``.

    Args:
        aadhaar_number: 12-digit Aadhaar number.
        otp:            6-digit OTP.
        request_id:     Request ID from ``_idfy_send_otp``.

    Returns:
        Demographic dict or None if OTP is wrong.

    Raises:
        RuntimeError: On provider timeout.
    """
    import httpx

    url = f"{settings.AADHAR_KYC_URL.rstrip('/')}/submit_aadhaar_otp"
    headers = {
        "api-key": settings.AADHAR_KYC_API_KEY,
        "account-id": settings.AADHAR_KYC_ACCOUNT_ID,
        "Content-Type": "application/json",
    }
    body = {
        "task_id": str(_uuid.uuid4()),
        "group_id": str(_uuid.uuid4()),
        "data": {
            "aadhaar_number": aadhaar_number,
            "otp": otp,
            "request_id": request_id,
        },
    }

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            logger.error("IDfy API timed out during OTP verification")
            raise RuntimeError("IDfy KYC provider timed out.")
        except httpx.HTTPError as e:
            logger.error("Cannot reach IDfy API: %s", type(e).__name__)
            raise RuntimeError("Cannot reach Aadhaar KYC provider. Check network.")

    if not resp.is_success:
        logger.warning(f"IDfy OTP verify failed {resp.status_code}: {resp.text[:200]}")
        return None

    aadhaar_data = (
        resp.json()
        .get("result", {})
        .get("entity", {})
        .get("aadhaar_data", {})
    )
    if not aadhaar_data:
        logger.warning("IDfy OTP verify: aadhaar_data missing in response")
        return None

    logger.info(f"IDfy OTP verified for {_masked(aadhaar_number)}")
    return {
        "verified": True,
        "name": aadhaar_data.get("name"),
        "gender": aadhaar_data.get("gender"),
        "dob": aadhaar_data.get("dob"),
        "state": aadhaar_data.get("address", {}).get("state"),
        "zip": aadhaar_data.get("zip"),
    }


# ── Public API ────────────────────────────────────────────────────────────────


def _require_kyc_config() -> None:
    """Raise unless the KYC provider is fully configured.

    Both the send and verify paths need this. Verify used to skip it, so a
    missing URL produced a 500 instead of a 503.

    Raises:
        RuntimeError: If the API key or base URL is missing.
    """
    if not settings.AADHAR_KYC_API_KEY:
        logger.error("AADHAR_KYC_API_KEY is not configured")
        raise RuntimeError("Aadhaar KYC is not configured")
    if not settings.AADHAR_KYC_URL:
        logger.error("AADHAR_KYC_URL is not configured")
        raise RuntimeError("Aadhaar KYC is not configured")


async def send_aadhar_otp(aadhaar_number: str, db) -> dict:
    """Request an OTP for the given Aadhaar number (provider-agnostic).

    Routes to the configured KYC provider based on ``AADHAAR_BACKEND``.
    With ``AADHAAR_BACKEND="console"``, uses a fully mocked implementation.

    Args:
        aadhaar_number: 12-digit Aadhaar number (pre-validated by caller).
        db:             SQLAlchemy session (used by the console backend only).

    Returns:
        Dict with ``client_id``, ``txn_id``, ``message`` (and ``dev_otp`` with
        the console backend).

    Raises:
        RuntimeError: On provider configuration error or API failure.
        ValueError:   On invalid Aadhaar number as per provider.
    """
    if settings.AADHAAR_BACKEND == "console":
        return await _dev_send_otp(aadhaar_number, db)

    _require_kyc_config()

    provider = settings.AADHAAR_BACKEND
    logger.info(f"Sending Aadhaar OTP via provider '{provider}' for {_masked(aadhaar_number)}")

    if provider == "surepass":
        return await _surepass_send_otp(aadhaar_number)
    elif provider == "idfy":
        return await _idfy_send_otp(aadhaar_number)
    else:
        raise RuntimeError(f"Unknown AADHAAR_BACKEND: '{provider}'. Use 'surepass' or 'idfy'.")


async def verify_aadhar_otp(aadhaar_number: str, otp: str, client_id: str, db) -> Optional[dict]:
    """Verify an Aadhaar OTP (provider-agnostic).

    Routes to the configured KYC provider. With ``AADHAAR_BACKEND="console"``,
    checks against the OTP stored in the database by ``send_aadhar_otp``.

    Args:
        aadhaar_number: 12-digit Aadhaar number.
        otp:            6-digit OTP from the Aadhaar-linked mobile.
        client_id:      Transaction ID returned by ``send_aadhar_otp``.
        db:             SQLAlchemy session (used by the console backend only).

    Returns:
        Demographic dict (verified, name, gender, dob, state, zip) on success,
        None if OTP is invalid or expired.

    Raises:
        RuntimeError: On provider configuration error or API failure.
    """
    if settings.AADHAAR_BACKEND == "console":
        return await _dev_verify_otp(aadhaar_number, otp, db)

    # This guard was missing while the send side had it. With AADHAR_KYC_URL
    # empty, httpx was handed a bare path like "/aadhaar-v2/submit-otp" and
    # raised UnsupportedProtocol — which is neither TimeoutException nor
    # ConnectError, so it escaped the handlers below and surfaced as an opaque
    # HTTP 500 to a user who had already received their OTP.
    _require_kyc_config()

    provider = settings.AADHAAR_BACKEND
    logger.info(f"Verifying Aadhaar OTP via provider '{provider}'")

    if provider == "surepass":
        # Surepass uses client_id (not aadhaar_number) for OTP verification
        return await _surepass_verify_otp(client_id, otp)
    elif provider == "idfy":
        return await _idfy_verify_otp(aadhaar_number, otp, client_id)
    else:
        raise RuntimeError(f"Unknown AADHAAR_BACKEND: '{provider}'. Use 'surepass' or 'idfy'.")