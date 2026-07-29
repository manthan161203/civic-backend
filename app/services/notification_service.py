"""
Notification Service — DB Persistence and FCM Push
===================================================
Saves notifications to the database and fires Firebase Cloud Messaging (FCM)
push notifications to mobile devices.

Key design points:
- Every notification is always persisted to the DB regardless of push outcome.
- FCM push is best-effort — failure is logged but does not raise exceptions.
- Supports multilingual messages (English, Hindi, Gujarati) via template keys.
- With PUSH_BACKEND="console", FCM pushes are only logged (no Firebase calls).

Usage:
    # Direct message (title + body already known):
    notify(db, user_id, "Title", "Body", "assignment", issue_id, fcm_token)

    # Templated message (uses localized strings based on user language):
    notify_localized(db, user, key="assignment", notification_type="assignment",
                     issue_id=str(issue.id), issue_type="pothole", ward="Ward 5")
"""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.core.config import settings
from app.core.logger import get_logger
from app.models.notification import Notification

logger = get_logger("notification")

_firebase_initialized = False

# ── Multilingual notification templates ──────────────────────────────────────
# Each key maps to {lang: (title_template, body_template)}.
# Use {placeholders} for dynamic values — filled at runtime via str.format(**kwargs).

_MESSAGES: dict[str, dict[str, tuple[str, str]]] = {
    "assignment": {
        "en": ("New task assigned", "A {issue_type} issue has been assigned to you in {ward}."),
        "hi": ("नया कार्य सौंपा गया", "{ward} में एक {issue_type} समस्या आपको सौंपी गई है।"),
        "gu": ("નવું કાર્ય સોંપાયું", "{ward} માં {issue_type} સમસ્યા તમને સોંપવામાં આવી છે।"),
    },
    "in_progress": {
        "en": ("Work started on your issue", "A worker has started working on your {issue_type} complaint in {ward}."),
        "hi": ("आपकी समस्या पर काम शुरू हुआ", "{ward} में आपकी {issue_type} शिकायत पर एक कर्मचारी ने काम शुरू किया है।"),
        "gu": ("તમારી સમસ્યા પર કામ શરૂ થયું", "{ward} માં તમારી {issue_type} ફરિયાદ પર કામ શરૂ થઈ ગયું છે।"),
    },
    "resolution": {
        "en": ("Issue resolved!", "Your {issue_type} complaint in {ward} has been resolved. Please rate the service."),
        "hi": ("समस्या हल हो गई!", "{ward} में आपकी {issue_type} शिकायत हल हो गई है। कृपया सेवा को रेटिंग दें।"),
        "gu": ("સમસ્યા ઉકેલાઈ!", "{ward} માં તમારી {issue_type} ફરિયાદ ઉકેલાઈ ગઈ છે। કૃપા કરી સેવાને રેટિંગ આપો।"),
    },
    "blocked": {
        "en": ("Task blocked — action needed", "Issue #{issue_id_short} is blocked: {reason}. Manual review required."),
        "hi": ("कार्य अवरुद्ध — कार्रवाई जरूरी", "समस्या #{issue_id_short} अवरुद्ध है: {reason}। मैन्युअल समीक्षा आवश्यक।"),
        "gu": ("કાર્ય અટક્યું — કાર્યવાહી જરૂરી", "સમસ્યા #{issue_id_short} અટકી ગઈ: {reason}। મૅન્યુઅલ સમીક્ષા જરૂરી।"),
    },
    "escalation": {
        "en": ("Issue auto-escalated", "Issue #{issue_id_short} ({issue_type}, {ward}) has been open for over 48 hours and was auto-escalated."),
        "hi": ("समस्या स्वतः एस्कलेट हुई", "समस्या #{issue_id_short} ({issue_type}, {ward}) 48 घंटे से अधिक खुली है और स्वतः एस्कलेट हो गई।"),
        "gu": ("સમસ્યા ઓટો-એસ્કેલેટ થઈ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) 48 કલાકથી વધુ ખુલ્લી છે અને ઓટો-એસ્કેલેટ થઈ ગઈ।"),
    },
    "rejection": {
        "en": ("Issue reassigned", "Your {issue_type} issue could not be handled by the previous worker and has been reassigned."),
        "hi": ("समस्या पुनः सौंपी गई", "आपकी {issue_type} समस्या पिछले कर्मचारी द्वारा नहीं संभाली जा सकी और पुनः सौंपी गई है।"),
        "gu": ("સમસ્યા ફરી સોંપવામાં આવી", "તમારી {issue_type} સમસ્યા અગાઉના કર્મચારી દ્વારા ન સંભળાઈ, ફરી સોંપવામાં આવી છે।"),
    },
    "poor_resolution": {
        "en": ("AI quality check failed", "Issue #{issue_id_short} ({issue_type}, {ward}) was marked resolved but AI detected the fix is incomplete. Please review."),
        "hi": ("AI गुणवत्ता जांच विफल", "समस्या #{issue_id_short} ({issue_type}, {ward}) हल चिह्नित की गई, पर AI ने अधूरा समाधान पाया। कृपया समीक्षा करें।"),
        "gu": ("AI ગુણવત્તા તપાસ નિષ્ફળ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) ઉકેલાઈ ગઈ ચિહ્નિત, પણ AI એ અધૂરો ઉકેલ શોધ્યો. કૃપા કરી સમીક્ષા કરો."),
    },
    "sos_alert": {
        "en": ("🚨 SOS Alert", "Critical SOS issue #{issue_id_short} ({issue_type}) reported at {address}. Immediate attention required."),
        "hi": ("🚨 SOS अलर्ट", "#{issue_id_short} ({issue_type}) पर गंभीर SOS समस्या {address} पर रिपोर्ट की गई। तत्काल ध्यान जरूरी।"),
        "gu": ("🚨 SOS ચેતવણી", "#{issue_id_short} ({issue_type}) ગંભીર SOS સમસ્યા {address} પર નોંધાઈ. તાત્કાલિક ધ્યાન જરૂરી."),
    },
    "comment": {
        "en": ("New update on your issue", "An official update was added to your {issue_type} complaint in {ward}."),
        "hi": ("आपकी समस्या पर नया अपडेट", "{ward} में आपकी {issue_type} शिकायत पर एक आधिकारिक अपडेट जोड़ा गया।"),
        "gu": ("તમારી સમસ્યા પર નવો અપડેટ", "{ward} માં તમારી {issue_type} ફરિયાદ પર સત્તાવાર અપડેટ ઉમેરવામાં આવ્યો."),
    },
    "reply": {
        "en": ("Someone replied to your comment", "Your comment on the {issue_type} issue in {ward} received a reply."),
        "hi": ("आपकी टिप्पणी पर जवाब", "{ward} में {issue_type} समस्या पर आपकी टिप्पणी का जवाब मिला।"),
        "gu": ("તમારી ટિપ્પણીનો જવાબ", "{ward} માં {issue_type} સમસ્યા પર તમારી ટિપ્પણીનો જવાબ મળ્યો."),
    },
    "rated": {
        "en": ("Your work was rated", "A citizen gave your {issue_type} resolution in {ward} a {rating}★ rating."),
        "hi": ("आपके काम को रेटिंग मिली", "एक नागरिक ने {ward} में {issue_type} समस्या के आपके समाधान को {rating}★ दिया।"),
        "gu": ("તમારા કામને રેટિંગ મળ્યું", "એક નાગરિકે {ward} માં {issue_type} સમસ્યા પર તમારા ઉકેલને {rating}★ રેટિંગ આપ્યું."),
    },
    "closed": {
        "en": ("Issue closed by citizen", "The citizen has closed issue #{issue_id_short} ({issue_type}, {ward})."),
        "hi": ("नागरिक द्वारा समस्या बंद", "नागरिक ने समस्या #{issue_id_short} ({issue_type}, {ward}) बंद की।"),
        "gu": ("નાગરિક દ્વારા સમસ્યા બંધ", "નાગરિકે સમસ્યા #{issue_id_short} ({issue_type}, {ward}) બંધ કરી."),
    },
    "reopened": {
        "en": ("Issue reopened", "Issue #{issue_id_short} ({issue_type}, {ward}) was reopened by the citizen and needs reassignment."),
        "hi": ("समस्या पुनः खोली गई", "समस्या #{issue_id_short} ({issue_type}, {ward}) नागरिक द्वारा पुनः खोली गई और पुन: सौंपने की जरूरत है।"),
        "gu": ("સમસ્યા ફરી ખોલવામાં આવી", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) નાગરિક દ્વારા ફરી ખોલવામાં આવી, ફરી સોંપણી જરૂરી."),
    },
    "dispute_opened": {
        "en": ("Resolution disputed", "A citizen has disputed the resolution of issue #{issue_id_short} ({issue_type}) in {ward}. Review required."),
        "hi": ("समाधान पर विवाद", "एक नागरिक ने {ward} में समस्या #{issue_id_short} ({issue_type}) के समाधान पर विवाद किया है। समीक्षा आवश्यक।"),
        "gu": ("ઉકેલ પર વિવાદ", "એક નાગરિકે {ward} માં સમસ્યા #{issue_id_short} ({issue_type}) ના ઉકેલ પર વિવાદ કર્યો છે. સમીક્ષા જરૂરી."),
    },
    "dispute_resolved": {
        "en": ("Dispute resolved", "Your dispute on issue #{issue_id_short} has been {outcome}."),
        "hi": ("विवाद सुलझाया गया", "समस्या #{issue_id_short} पर आपका विवाद {outcome} कर दिया गया है।"),
        "gu": ("વિવાદ ઉકેલાયો", "સમસ્યા #{issue_id_short} પર તમારો વિવાદ {outcome} કરવામાં આવ્યો છે."),
    },
    "worker_complaint": {
        "en": ("Worker complaint received", "A complaint has been filed against worker in {ward} for issue #{issue_id_short}. Review required."),
        "hi": ("कर्मचारी शिकायत प्राप्त", "{ward} में समस्या #{issue_id_short} के लिए कर्मचारी के खिलाफ शिकायत दर्ज की गई। समीक्षा आवश्यक।"),
        "gu": ("કર્મચારી ફરિયાદ મળી", "{ward} માં સમસ્યા #{issue_id_short} માટે કર્મચારી સામે ફરિયાદ નોંધાઈ. સમીક્ષા જરૂરી."),
    },
    "escalation_ward": {
        "en": ("Issue escalated to ward admin", "Issue #{issue_id_short} ({issue_type}, {ward}) has breached SLA ({sla_hours}h). Escalated to ward level."),
        "hi": ("समस्या वार्ड एडमिन को एस्कलेट", "समस्या #{issue_id_short} ({issue_type}, {ward}) ने SLA ({sla_hours}h) का उल्लंघन किया। वार्ड स्तर पर एस्कलेट।"),
        "gu": ("સમસ્યા વોર્ડ એડમિનને એસ્કેલેટ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) SLA ({sla_hours}h) ભંગ. વોર્ડ સ્તરે એસ્કેલેટ."),
    },
    "escalation_taluka": {
        "en": ("Issue escalated to taluka admin", "Issue #{issue_id_short} ({issue_type}, {ward}) breached 2x SLA. Escalated to taluka level."),
        "hi": ("समस्या तालुका एडमिन को एस्कलेट", "समस्या #{issue_id_short} ({issue_type}, {ward}) ने 2x SLA का उल्लंघन किया। तालुका स्तर पर एस्कलेट।"),
        "gu": ("સમસ્યા તાલુકા એડમિનને એસ્કેલેટ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) 2x SLA ભંગ. તાલુકા સ્તરે એસ્કેલેટ."),
    },
    "escalation_district": {
        "en": ("Issue escalated to district admin", "Issue #{issue_id_short} ({issue_type}, {ward}) breached 3x SLA. Critical escalation to district level."),
        "hi": ("समस्या जिला एडमिन को एस्कलेट", "समस्या #{issue_id_short} ({issue_type}, {ward}) ने 3x SLA का उल्लंघन किया। जिला स्तर पर गंभीर एस्कलेशन।"),
        "gu": ("સમસ્યા જિલ્લા એડમિનને એસ્કેલેટ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) 3x SLA ભંગ. જિલ્લા સ્તરે ગંભીર એસ્કેલેશન."),
    },
    "bookmark_update": {
        "en": ("Bookmarked issue updated", "An issue you bookmarked (#{issue_id_short}, {issue_type}) in {ward} changed status to {new_status}."),
        "hi": ("बुकमार्क समस्या अपडेट", "आपकी बुकमार्क समस्या (#{issue_id_short}, {issue_type}) {ward} में {new_status} हो गई।"),
        "gu": ("બુકમાર્ક સમસ્યા અપડેટ", "તમારી બુકમાર્ક સમસ્યા (#{issue_id_short}, {issue_type}) {ward} માં {new_status} થઈ ગઈ."),
    },
    "sla_violation": {
        "en": ("SLA violation", "Issue #{issue_id_short} ({issue_type}, {ward}) — {priority} priority — breached {sla_hours}h SLA. Overdue by {hours_overdue}h."),
        "hi": ("SLA उल्लंघन", "समस्या #{issue_id_short} ({issue_type}, {ward}) — {priority} प्राथमिकता — {sla_hours}h SLA का उल्लंघन। {hours_overdue}h विलंब।"),
        "gu": ("SLA ભંગ", "સમસ્યા #{issue_id_short} ({issue_type}, {ward}) — {priority} પ્રાથમિકતા — {sla_hours}h SLA ભંગ. {hours_overdue}h વિલંબ."),
    },
}

# SMS templates for key lifecycle events (short, SMS-friendly)
_SMS_TEMPLATES: dict[str, dict[str, str]] = {
    "assigned": {
        "en": "Your {issue_type} report #{issue_id_short} has been assigned to a worker. Track it in the Civic app.",
        "hi": "आपकी {issue_type} रिपोर्ट #{issue_id_short} एक कर्मचारी को सौंपी गई। Civic ऐप में ट्रैक करें।",
        "gu": "તમારી {issue_type} ફરિયાદ #{issue_id_short} કર્મચારીને સોંપાઈ. Civic એપમાં ટ્રૅક કરો.",
    },
    "in_progress": {
        "en": "Work started on your {issue_type} report #{issue_id_short}. A worker is on it!",
        "hi": "आपकी {issue_type} रिपोर्ट #{issue_id_short} पर काम शुरू हुआ। कर्मचारी काम कर रहा है!",
        "gu": "તમારી {issue_type} ફરિયાદ #{issue_id_short} પર કામ શરૂ. કર્મચારી કામ કરી રહ્યો છે!",
    },
    "resolved": {
        "en": "Your {issue_type} issue #{issue_id_short} has been resolved! Open the Civic app to rate the service.",
        "hi": "आपकी {issue_type} समस्या #{issue_id_short} हल हो गई! सेवा रेट करने के लिए Civic ऐप खोलें।",
        "gu": "તમારી {issue_type} સમસ્યા #{issue_id_short} ઉકેલાઈ! સેવા રેટ કરવા Civic એપ ખોલો.",
    },
}


def _localize(lang: Optional[str], key: str, **kwargs) -> tuple[str, str]:
    """Resolve a notification template key to (title, body) in the user's language.

    Falls back to English if the language is not supported or the template is missing.

    Args:
        lang:    User's preferred language — ``"en"``, ``"hi"``, or ``"gu"``.
        key:     Template key (e.g. ``"assignment"``, ``"resolution"``).
        **kwargs: Placeholder values to substitute in the template strings.

    Returns:
        Tuple of (title, body) with placeholders filled in.
    """
    lang = lang if lang in ("en", "hi", "gu") else "en"
    title_tpl, body_tpl = _MESSAGES.get(key, {}).get(lang) or _MESSAGES[key]["en"]
    return title_tpl.format(**kwargs), body_tpl.format(**kwargs)


def notify(
    db: Session,
    user_id: str,
    title: str,
    body: str,
    notification_type: str,
    issue_id: Optional[str] = None,
    fcm_token: Optional[str] = None,
    location_lat: Optional[float] = None,
    location_lng: Optional[float] = None,
    action_type: Optional[str] = None,
) -> Notification:
    """Save a notification to DB and optionally fire an FCM push.

    FIX HIGH PRIORITY BUG #11: Notification broadcasting race condition
    Added idempotency check to prevent duplicate notifications for assignment broadcasts.
    
    The DB record is always created. FCM push is only attempted if ``fcm_token``
    is provided. FCM failure is logged but does not raise or block the DB write.

    Args:
        db:                SQLAlchemy database session.
        user_id:           Target user's UUID string.
        title:             Notification title.
        body:              Notification body text.
        notification_type: Category — ``"status_update"``, ``"assignment"``,
                           ``"resolution"``, or ``"system"``.
        issue_id:          Related issue UUID string (optional).
        fcm_token:         Device FCM token for push (optional).
        location_lat:      Latitude of an attached map location (optional).
        location_lng:      Longitude of an attached map location (optional).

    Returns:
        The saved ``Notification`` ORM object.
    """
    # Check for duplicate assignment notifications (race condition prevention)
    # Only check for assignment notifications to avoid false duplicates
    if notification_type == "assignment" and issue_id:
        existing = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.issue_id == issue_id,
            Notification.type == "assignment",
            Notification.created_at >= (now_utc() - timedelta(seconds=5))  # Within 5 seconds
        ).first()
        if existing:
            logger.info(
                "Duplicate notification prevented (idempotency)",
                extra={"user_id": user_id, "issue_id": issue_id, "type": notification_type}
            )
            return existing
    
    notif = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        issue_id=issue_id,
        title=title,
        body=body,
        type=notification_type,
        action_type=action_type,
        location_lat=location_lat,
        location_lng=location_lng,
    )
    try:
        db.add(notif)
        db.commit()
        logger.info(f"Notification saved for user {user_id}: [{notification_type}] {title}")
    except Exception as e:
        logger.error(f"Failed to save notification for user {user_id}: {e}", exc_info=True)
        raise

    if fcm_token:
        data = {}
        if location_lat is not None and location_lng is not None:
            data["location_lat"] = str(location_lat)
            data["location_lng"] = str(location_lng)
        _send_fcm(fcm_token, title, body, data=data or None)

    return notif


def notify_localized(
    db: Session,
    user,
    key: str,
    notification_type: str,
    issue_id: Optional[str] = None,
    action_type: Optional[str] = None,
    **kwargs,
) -> Notification:
    """Send a notification using a localized message template key.

    Looks up the message template by ``key``, formats it in the user's
    preferred language, then calls ``notify()``.

    Args:
        db:                SQLAlchemy database session.
        user:              User ORM object (must have ``.id``, ``.language``, ``.fcm_token``).
        key:               Template key — one of: ``"assignment"``, ``"in_progress"``,
                           ``"resolution"``, ``"blocked"``, ``"escalation"``,
                           ``"rejection"``, ``"poor_resolution"``, etc.
        notification_type: Category for the DB record.
        issue_id:          Related issue UUID string (optional).
        action_type:       Deep link action for mobile app (e.g. ``"open_issue"``).
        **kwargs:          Template placeholder values (e.g. ``issue_type="pothole"``).

    Returns:
        The saved ``Notification`` ORM object.
    """
    title, body = _localize(getattr(user, "language", "en"), key, **kwargs)
    return notify(
        db=db,
        user_id=str(user.id),
        title=title,
        body=body,
        notification_type=notification_type,
        issue_id=issue_id,
        fcm_token=getattr(user, "fcm_token", None),
        action_type=action_type,
    )


def _init_firebase() -> bool:
    """Initialize Firebase Admin SDK (singleton).

    Reads the service account JSON from the path in ``FIREBASE_CREDENTIALS_PATH``.
    Safe to call multiple times — only initializes once.

    Returns:
        True if Firebase is ready, False if credentials are missing or init failed.
    """
    global _firebase_initialized
    if _firebase_initialized:
        return True

    cred_path = Path(settings.FIREBASE_CREDENTIALS_PATH)
    if not cred_path.exists():
        logger.warning(f"Firebase credentials not found at '{cred_path}' — FCM push disabled")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            cred = credentials.Certificate(str(cred_path))
            firebase_admin.initialize_app(cred)
        _firebase_initialized = True
        logger.info("Firebase Admin SDK initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Firebase Admin SDK initialization failed: {e}", exc_info=True)
        return False


def notify_ward_subscribers(
    db,
    issue,
    title: str,
    body: str,
) -> int:
    """Send push notifications to all citizens subscribed to the issue's ward.

    Called when a new issue is created in a ward. Citizens who have subscribed
    to that ward receive a push notification (but no DB notification record —
    only FCM push to avoid inbox noise).

    Args:
        db:    SQLAlchemy database session.
        issue: Issue ORM object (must have ``ward_id`` or ``ward`` set).
        title: Notification title.
        body:  Notification body.

    Returns:
        Number of push notifications attempted.
    """
    try:
        from app.models.user import User
        from app.models.ward_subscription import WardSubscription

        if issue.ward_id:
            subs = (
                db.query(WardSubscription)
                .filter(WardSubscription.ward_id == issue.ward_id)
                .all()
            )
        else:
            # No structured ward_id — skip subscription push
            return 0

        count = 0
        for sub in subs:
            user = db.query(User).filter(User.id == sub.user_id, User.is_active == True).first()
            # Don't notify the reporter themselves (they already know)
            if user and user.fcm_token and str(user.id) != str(issue.reporter_id):
                _send_fcm(user.fcm_token, title, body)
                count += 1

        if count:
            logger.info(f"Ward subscription push: {count} notifications for issue {issue.id} in ward {issue.ward_id}")
        return count
    except Exception as e:
        logger.warning(f"Ward subscriber push failed (non-fatal): {e}")
        return 0


def _send_fcm(token: str, title: str, body: str, data: Optional[dict] = None) -> bool:
    """Send a push notification via Firebase Cloud Messaging (FCM v1 API).

    With ``PUSH_BACKEND="console"``, only logs without calling Firebase.
    Failure is logged and returned — callers are not blocked, because the
    notification row is written to the database first either way.

    Args:
        token: Target device FCM registration token.
        title: Notification title.
        body:  Notification body text.
        data:  Optional key-value data payload (all values must be strings).

    Returns:
        True on success (or with the console backend), False on failure.
    """
    if settings.PUSH_BACKEND == "console":
        logger.info(f"[console] FCM push → '{title}': {body}")
        return True

    if not _init_firebase():
        return False

    try:
        from firebase_admin import messaging

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data or {},
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default")
                )
            ),
            token=token,
        )
        response = messaging.send(message)
        logger.info(f"FCM push sent successfully: {response}")
        return True
    except Exception as e:
        logger.error(f"FCM push failed for token ...{token[-6:]}: {e}", exc_info=True)
        return False


# ── SMS Status Notifications ─────────────────────────────────────────────────

def send_sms_status_update(
    phone: Optional[str],
    lang: Optional[str],
    sms_key: str,
    **kwargs,
) -> bool:
    """Send an SMS status update for key issue lifecycle events.

    Uses MSG91 via the existing SMS service. Only sends for keys that
    have SMS templates defined (assigned, in_progress, resolved).

    Args:
        phone: User's phone number (with country code).
        lang:  User's preferred language.
        sms_key: Template key matching _SMS_TEMPLATES.
        **kwargs: Placeholder values for the template.

    Returns:
        True if SMS was sent/logged, False otherwise.
    """
    if not phone:
        return False

    template = _SMS_TEMPLATES.get(sms_key)
    if not template:
        return False

    lang = lang if lang in ("en", "hi", "gu") else "en"
    message = template.get(lang, template["en"]).format(**kwargs)

    if settings.SMS_BACKEND == "console":
        logger.info(f"[console] SMS → {phone}: {message}")
        return True

    # Not `return True`: an unconfigured MSG91 means the status update was never
    # delivered, and the caller should be able to tell that from the return value.
    if not settings.MSG91_API_KEY or not settings.MSG91_TEMPLATE_ID:
        logger.error("MSG91 is not configured — cannot send status SMS to %s", phone)
        return False

    try:
        import httpx
        headers = {
            "authkey": settings.MSG91_API_KEY,
            "accept": "application/json",
            "content-type": "application/json",
        }
        payload = {
            "flow_id": settings.MSG91_TEMPLATE_ID,
            "mobiles": phone.lstrip("+"),
            "message": message,
        }
        # Best-effort SMS send — don't block on failure
        with httpx.Client(timeout=10) as client:
            resp = client.post("https://control.msg91.com/api/v5/flow/", json=payload, headers=headers)
            if resp.status_code == 200:
                logger.info(f"SMS status update sent to {phone}")
                return True
            logger.warning(f"SMS send failed for {phone}: {resp.status_code}")
    except Exception as e:
        logger.warning(f"SMS send failed for {phone}: {e}")
    return False


# ── Bookmark Status Notifications ────────────────────────────────────────────

def notify_bookmarkers(db: Session, issue, new_status: str) -> int:
    """Notify all users who bookmarked an issue when its status changes.

    Args:
        db:         SQLAlchemy session.
        issue:      Issue ORM object.
        new_status: The new status string.

    Returns:
        Number of notifications sent.
    """
    try:
        from app.models.issue_bookmark import IssueBookmark
        from app.models.user import User

        bookmarks = (
            db.query(IssueBookmark)
            .filter(IssueBookmark.issue_id == issue.id)
            .all()
        )
        count = 0
        for bm in bookmarks:
            # Don't notify the reporter (they get their own notification)
            if str(bm.user_id) == str(issue.reporter_id):
                continue
            user = db.query(User).filter(User.id == bm.user_id, User.is_active == True).first()
            if user:
                notify_localized(
                    db=db, user=user, key="bookmark_update",
                    notification_type="status_update",
                    issue_id=str(issue.id),
                    action_type="open_issue",
                    issue_id_short=str(issue.id)[:8],
                    issue_type=issue.issue_type,
                    ward=issue.ward or "your area",
                    new_status=new_status,
                )
                count += 1
        return count
    except Exception as e:
        logger.warning(f"Bookmark notification failed (non-fatal): {e}")
        return 0