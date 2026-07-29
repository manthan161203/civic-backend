"""Email Service — Worker Invitations & Password Resets
=======================================================

Sends HTML and plaintext emails via SMTP. With ``EMAIL_BACKEND="console"`` the
message is logged instead of sent.

Main function:
    send_worker_invitation(to_email, worker_name, phone, temp_password) -> bool

Returns True on success, False on failure. A missing SMTP configuration is a
failure, not a success — the caller needs to know the worker never received
their credentials.

Temporary passwords are never written to the log. Log files are retained for 30
days and the Sentry logging integration ships INFO-level records as breadcrumbs,
so a password logged here outlives and outruns the email it came from.
"""

from app.core.time import now_utc
from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("email_service")


async def send_worker_invitation(
    to_email: str,
    worker_name: str,
    phone: str,
    temp_password: str,
) -> bool:
    """Send worker invitation email with temporary password.

    Args:
        to_email: Recipient email address.
        worker_name: Worker's display name (optional, used in greeting).
        phone: Worker's phone number (included in email).
        temp_password: Temporary password (included in email).

    Returns:
        True if the email was sent (or logged by the console backend), False on
        failure — including when SMTP is not configured.

    With ``EMAIL_BACKEND="console"`` the message is logged and the password is
    redacted. The caller receives the plaintext password back and is responsible
    for surfacing it; see ``POST /admin/workers``.
    """

    if settings.EMAIL_BACKEND == "console":
        logger.info(
            f"[console] Worker invitation email (not actually sent):\n"
            f"  To: {to_email}\n"
            f"  Phone: {phone}\n"
            f"  Temp Password: ***redacted — returned in the API response***\n"
            f"  Expires: {_format_expiry_date()}"
        )
        return True

    # Previously this returned True, so POST /admin/workers answered 201 while
    # the worker never received anything and the only copy of their password was
    # a log line.
    if not settings.SMTP_HOST:
        logger.error(
            "SMTP_HOST is not configured — cannot send the worker invitation to %s",
            to_email,
        )
        return False

    try:
        import aiosmtplib
    except ImportError:
        logger.error(
            "aiosmtplib is not installed. Cannot send email.",
            exc_info=True,
        )
        return False

    try:
        import ssl
        subject = "Your Civic Worker Account Invitation"
        html_body = _build_html_email(worker_name, phone, temp_password)
        text_body = _build_text_email(worker_name, phone, temp_password)

        # Connect and send via SMTP
        # Gmail on port 587: requires STARTTLS after bare connection
        # Gmail on port 465: requires SSL/TLS from start
        
        # Create SSL context for secure connection
        context = ssl.create_default_context()
        
        async with aiosmtplib.SMTP(
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
        ) as smtp:
            # For port 587: use STARTTLS 
            if settings.SMTP_PORT == 587:
                try:
                    await smtp.starttls(ssl_context=context)
                except Exception:
                    # If already TLS, just continue
                    pass
            
            await smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            await smtp.sendmail(
                settings.SMTP_FROM,  # sender (positional)
                [to_email],          # recipients (positional)
                _build_message(
                    subject=subject,
                    from_addr=settings.SMTP_FROM,
                    to_addr=to_email,
                    text_body=text_body,
                    html_body=html_body,
                )  # message (positional)
            )

        logger.info(f"Worker invitation email sent to {to_email}")
        return True

    except Exception as e:
        # Detailed error logging for production troubleshooting
        error_type = type(e).__name__
        error_msg = str(e)
        
        logger.error(
            f"Failed to send worker invitation email to {to_email}",
            extra={
                "error_type": error_type,
                "error_message": error_msg,
                "smtp_host": settings.SMTP_HOST,
                "smtp_port": settings.SMTP_PORT,
                "recipient_email": to_email,
                "email_backend": settings.EMAIL_BACKEND,
            },
            exc_info=True
        )
        
        # Log specific error types for easier debugging
        if "Authentication" in error_type or "401" in str(e):
            logger.error("SMTP Authentication Failed - Check SMTP_USER and SMTP_PASSWORD")
        elif "Connection" in error_type or "refused" in error_msg.lower():
            logger.error(f"SMTP Connection Failed - Cannot reach {settings.SMTP_HOST}:{settings.SMTP_PORT}")
        elif "timeout" in error_msg.lower():
            logger.error(f"SMTP Timeout - Connection took too long to {settings.SMTP_HOST}")
        elif "TLS" in error_type or "SSL" in error_type:
            logger.error(f"SMTP TLS/SSL Error - Check SMTP_TLS setting (current: {settings.SMTP_TLS})")
        
        return False


def _build_message(
    subject: str,
    from_addr: str,
    to_addr: str,
    text_body: str,
    html_body: str,
) -> str:
    """Build a multipart MIME message with both plaintext and HTML versions.

    Uses base64 encoding for UTF-8 content to avoid Unicode issues.
    Returns a formatted email message string suitable for SMTP sendmail().
    """
    import base64
    import uuid
    from email.header import Header

    boundary = str(uuid.uuid4())
    
    # Encode subject for UTF-8
    subject_encoded = Header(subject, 'utf-8').encode()
    
    # Base64 encode the text and HTML bodies
    text_b64 = base64.b64encode(text_body.encode('utf-8')).decode('ascii')
    html_b64 = base64.b64encode(html_body.encode('utf-8')).decode('ascii')

    msg = f"""From: {from_addr}
To: {to_addr}
Subject: {subject_encoded}
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="{boundary}"

--{boundary}
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: base64

{text_b64}

--{boundary}
Content-Type: text/html; charset="utf-8"
Content-Transfer-Encoding: base64

{html_b64}

--{boundary}--
"""
    return msg


def _build_text_email(worker_name: str, phone: str, temp_password: str) -> str:
    """Build plaintext version of the invitation email."""
    greeting = f"Hello {worker_name.title()}," if worker_name and worker_name.strip() else "Hello,"

    return f"""{greeting}

You have been invited to join Civic as a Worker. Here are your login credentials:

Phone: {phone}
Temporary Password: {temp_password}

This password is randomly generated and only visible in this email. Please keep it secure and change it immediately after logging in.

IMPORTANT: You must change your password within 7 days of receiving this email.
Your account will be deactivated if you do not change your password by {_format_expiry_date()}.

How to get started:
1. Download the Civic mobile app from the App Store or Google Play.
2. Open the app and log in with your phone number and temporary password, or use OTP if available.
3. Change your password immediately when prompted.
4. Complete your profile and begin accepting tasks.

Need help? Contact your administrator or email {settings.SUPPORT_EMAIL}.

Useful links:
- App download: {settings.APP_DOWNLOAD_URL}
- Help center: {settings.APP_HELP_URL}

Best regards,
The Civic Team"""


def _build_html_email(worker_name: str, phone: str, temp_password: str) -> str:
    """Build HTML version of the invitation email."""
    greeting = f"Hello <strong>{worker_name.title()}</strong>," if worker_name and worker_name.strip() else "Hello,"
    expiry_date = _format_expiry_date()

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #1a56db; color: white; padding: 20px; border-radius: 8px; text-align: center; }}
        .content {{ background-color: #f9fafb; padding: 30px; border-radius: 8px; margin-top: 20px; }}
        .credentials {{ background-color: white; border: 2px solid #e5e7eb; padding: 20px; border-radius: 6px; margin: 20px 0; }}
        .credential-row {{ margin: 12px 0; font-family: monospace; }}
        .label {{ color: #6b7280; font-weight: 600; }}
        .value {{ color: #000; font-size: 16px; margin-top: 4px; }}
        .warning {{ background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 12px; margin: 20px 0; border-radius: 4px; }}
        .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 30px; }}
        .button {{ display: inline-block; background-color: #1a56db; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 style="margin: 0;">Welcome to Civic</h1>
            <p style="margin: 10px 0 0 0;">Worker Account Invitation</p>
        </div>

        <div class="content">
            <p>{greeting}</p>
            <p>You have been invited to join <strong>Civic</strong> as a Worker. Below are your temporary login credentials:</p>

            <div class="credentials">
                <div class="credential-row">
                    <div class="label">Phone Number:</div>
                    <div class="value">{phone}</div>
                </div>
                <div class="credential-row">
                    <div class="label">Temporary Password:</div>
                    <div class="value">{temp_password}</div>
                </div>
            </div>

            <div class="warning">
                <strong style="color: #d97706;">⚠️ Important:</strong>
                <p style="margin: 8px 0 0 0; color: #b45309;">You must change your password within <strong>7 days</strong> of receiving this email. Your account will be deactivated if you do not change your password by <strong>{expiry_date}</strong>.</p>
            </div>

            <h3 style="color: #1a56db;">Getting Started:</h3>
            <ol>
                <li>Download the Civic mobile app from the App Store or Google Play.</li>
                <li>Open the app and log in with your phone number and temporary password, or use OTP if available.</li>
                <li>Change your password immediately when prompted.</li>
                <li>Complete your profile and start accepting tasks.</li>
            </ol>

            <div style="margin-top: 20px;">
                <a href="{settings.APP_DOWNLOAD_URL}" class="button">Download the App</a>
            </div>

            <p style="margin-top: 24px; color: #374151;">This password is randomly generated and only visible in this email. Keep it secure and do not share it.</p>
            <p style="margin-top: 18px; color: #6b7280;">Need help? Contact your administrator or email <a href="mailto:{settings.SUPPORT_EMAIL}" style="color: #1a56db; text-decoration: none;">{settings.SUPPORT_EMAIL}</a>.</p>
        </div>

        <div class="footer">
            <p>© Civic Platform. All rights reserved.</p>
            <p>This is an automated email. Please do not reply directly to this message.</p>
        </div>
    </div>
</body>
</html>"""


def _format_expiry_date() -> str:
    """Return the 7-day expiry date in a readable format."""
    from datetime import timedelta

    expiry = now_utc() + timedelta(days=7)
    return expiry.strftime("%B %d, %Y")
