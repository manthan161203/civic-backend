"""
Application Configuration
==========================
All environment variables are loaded from the ``.env`` file via Pydantic Settings.

Copy ``.env.example`` to ``.env`` and fill in your values before running.

Required variables:
    DATABASE_URL       PostgreSQL connection string
    SECRET_KEY         Random 32+ char secret for JWT signing

Optional integrations (features degrade gracefully if not set):
    GROQ_API_KEY       Fast text inference for chat and SQL agent (Groq)
    GEMINI_API_KEY     Image classification and resolution verification (Google Gemini)
    STORAGE_BACKEND    Image storage driver: "local" | "cloudinary" | "supabase"
    CLOUDINARY_*       Cloudinary credentials (used when STORAGE_BACKEND=cloudinary)
    SUPABASE_*         Supabase Storage credentials (used when STORAGE_BACKEND=supabase)
    MSG91_*            SMS OTP delivery (logged to console in DEV_MODE)
    FIREBASE_*         FCM push notifications (logged to console in DEV_MODE)
    GOOGLE_CLIENT_ID   Google OAuth Sign-In
    AADHAR_KYC_*       Aadhaar KYC OTP authentication
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file.

    Attributes:
        DATABASE_URL:               PostgreSQL connection URL.
                                    e.g. ``postgresql://user:pass@localhost/civic``
        SECRET_KEY:                 Secret key for JWT signing (keep this private).
        ALGORITHM:                  JWT signing algorithm (default: ``HS256``).
        ACCESS_TOKEN_EXPIRE_MINUTES: Access token lifetime in minutes (default: 15).

        STORAGE_BACKEND:            Image storage driver.
                                    ``"local"``      — filesystem under uploads/ (dev).
                                    ``"cloudinary"`` — Cloudinary CDN.
                                    ``"supabase"``   — Supabase Storage bucket.

        CLOUDINARY_CLOUD_NAME:      Cloudinary cloud name (used when STORAGE_BACKEND=cloudinary).
        CLOUDINARY_API_KEY:         Cloudinary API key.
        CLOUDINARY_API_SECRET:      Cloudinary API secret.

        SUPABASE_URL:               Supabase project URL (used when STORAGE_BACKEND=supabase).
        SUPABASE_SERVICE_KEY:       Supabase service_role key (not the anon key).
        SUPABASE_STORAGE_BUCKET:    Bucket name in Supabase Storage (default: civic-photos).

        GROQ_API_KEY:               Groq API key for fast chat and SQL agent inference.
        GEMINI_API_KEY:             Google Gemini API key for image classification and verification.

        MSG91_API_KEY:              MSG91 API key for SMS OTP delivery.
        MSG91_TEMPLATE_ID:          MSG91 approved OTP message template ID.

        FIREBASE_CREDENTIALS_PATH:  Path to Firebase Admin SDK JSON credentials file.
                                    Default: ``credentials/firebase-adminsdk.json``

        GOOGLE_CLIENT_ID:           Google OAuth2 client ID for ID token verification.

        AADHAR_KYC_PROVIDER:        KYC provider for Aadhaar — ``"surepass"`` or ``"idfy"``.
        AADHAR_KYC_API_KEY:         API key/token for the KYC provider.
        AADHAR_KYC_URL:             Base URL of the KYC provider API.
        AADHAR_KYC_ACCOUNT_ID:      IDfy-only account identifier.

        CORS_ORIGINS:               Comma-separated allowed origins, or ``"*"`` to allow all.
                                    Example: ``"https://app.example.com,https://admin.example.com"``

        DEV_MODE:                   When ``True``, disables real SMS/FCM/KYC calls
                                    and saves images locally instead of Cloudinary.
                                    **Always set to ``False`` in production.**
    """

    # Core
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # Increased from 15 for better dev UX

    # Storage backend — "local" | "cloudinary" | "supabase"
    STORAGE_BACKEND: str = "local"

    # Storage — Cloudinary (used when STORAGE_BACKEND=cloudinary)
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Storage — Supabase Storage (used when STORAGE_BACKEND=supabase)
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""           # service_role key (not anon key)
    SUPABASE_STORAGE_BUCKET: str = "civic-photos"

    # AI — Groq (chat, SQL agent)
    GROQ_API_KEY: str = ""

    # AI — Google Gemini (image classification, resolution verification)
    GEMINI_API_KEY: str = ""

    # SMS — MSG91 (OTP delivery)
    MSG91_API_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""

    # Push notifications — Firebase FCM
    FIREBASE_CREDENTIALS_PATH: str = "credentials/firebase-adminsdk.json"

    # Email — SMTP (worker invitation, password reset)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@civicapp.in"
    SMTP_TLS: bool = True

    # App URLs and support (used in worker invitation emails)
    APP_DOWNLOAD_URL: str = "https://civicapp.in/download"
    APP_HELP_URL: str = "https://civicapp.in/help"
    SUPPORT_EMAIL: str = "support@civicapp.in"

    # Google OAuth (mobile: client sends id_token, backend verifies signature)
    GOOGLE_CLIENT_ID: str = ""

    # Aadhaar KYC provider
    AADHAR_KYC_PROVIDER: str = "surepass"   # "surepass" or "idfy"
    AADHAR_KYC_API_KEY: str = ""
    AADHAR_KYC_URL: str = ""                # e.g. https://kyc-api.surepass.io/api/v1
    AADHAR_KYC_ACCOUNT_ID: str = ""         # IDfy only

    # CORS
    CORS_ORIGINS: str = "*"                 # "*" = allow all (dev only); use explicit origins in prod

    DEV_MODE: bool = True                  # Set True only for local development

    # Rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60         # default requests/min per IP

    # Environment and version
    ENVIRONMENT: str = "development"        # development or production
    APP_VERSION: str = "1.0.0"             # Application version

    model_config = {"env_file": ".env"}


settings = Settings()