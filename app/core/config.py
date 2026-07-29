"""
Application Configuration
==========================
All environment variables are loaded from the ``.env`` file via Pydantic Settings.

Copy ``.env.example`` to ``.env`` and fill in your values before running.

Required variables:
    DATABASE_URL       PostgreSQL connection string
    SECRET_KEY         Random 32+ char secret for JWT signing

How environments work
---------------------
``ENVIRONMENT`` (``development`` | ``production``) is the gate. It does not by
itself turn features on and off — it decides the *default* for each independent
switch, and it decides whether a bad configuration is a warning or a refusal to
start (see :func:`validate_settings`).

Every switch can be set explicitly to override that default. The switches are
deliberately separate because they used to be a single ``DEV_MODE`` boolean
controlling eleven unrelated things — docs exposure, HSTS, static file serving,
whether the OTP is echoed in the API response, and whether Google sign-in
accepts any string as a valid token. Bundling those together meant you could not
turn one on without turning them all on.

    Integration backends   SMS_BACKEND, EMAIL_BACKEND, PUSH_BACKEND,
                           AADHAAR_BACKEND, GOOGLE_AUTH_BACKEND
                           Each is either the real provider or ``"console"``,
                           which logs what would have been sent. ``"console"``
                           is forbidden in production.

    Exposure switches      DOCS_ENABLED, OTP_ECHO_IN_RESPONSE,
                           POOL_HEALTH_ENDPOINT_ENABLED
                           Unset (``None``) means "use the environment default".
                           Left unset they are on in development, off in
                           production.

``DEV_MODE`` is retained as a deprecated alias. Setting it true in development
still switches every backend to ``"console"`` and turns the exposure switches
on, so an existing ``.env`` keeps working; it emits a DeprecationWarning naming
the replacement. Setting it true in production is a hard error.

Optional integrations (features degrade gracefully if not set):
    GROQ_API_KEY       Fast text inference for chat and SQL agent (Groq)
    GEMINI_API_KEY     Image classification and resolution verification (Gemini)
    STORAGE_BACKEND    Image storage driver: "local" | "cloudinary"
    CLOUDINARY_*       Cloudinary credentials (used when STORAGE_BACKEND=cloudinary)
    MSG91_*            SMS OTP delivery
    FIREBASE_*         FCM push notifications
    GOOGLE_CLIENT_ID   Google OAuth Sign-In
    AADHAR_KYC_*       Aadhaar KYC OTP authentication
"""

import warnings
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings

Environment = Literal["development", "production"]

# Values that must never survive into a real deployment.
_PLACEHOLDER_SECRETS = {
    "your-super-secret-key-min-32-chars",
    "change_me_run_openssl_rand_hex_32",
    "changeme",
    "change-me",
    "secret",
    "secretkey",
    "dev",
    "development",
    "test",
    "testing",
}


# Database passwords that must never reach production. "civic" is the compose
# fallback, so it is what you get by deploying with no .env at all.
_WEAK_DB_PASSWORDS = {
    "civic", "postgres", "password", "changeme", "change_me", "secret", "admin",
    "root", "test", "dev",
}


class ConfigurationError(RuntimeError):
    """Raised when the configuration is unsafe or incomplete for the environment."""


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file.

    Attributes:
        ENVIRONMENT:                ``"development"`` or ``"production"``. Decides
                                    the default for every switch below and whether
                                    :func:`validate_settings` warns or refuses to start.
        APP_VERSION:                Reported to Sentry as the release identifier.

        DATABASE_URL:               PostgreSQL connection URL.
        SECRET_KEY:                 Secret key for JWT signing (keep this private).
        ALGORITHM:                  JWT signing algorithm (default: ``HS256``).
        ACCESS_TOKEN_EXPIRE_MINUTES: Access token lifetime in minutes.

        SMS_BACKEND:                ``"msg91"`` or ``"console"`` (logs the OTP).
        EMAIL_BACKEND:              ``"smtp"`` or ``"console"``.
        PUSH_BACKEND:               ``"fcm"`` or ``"console"``.
        AADHAAR_BACKEND:            ``"surepass"``, ``"idfy"`` or ``"console"``.
        GOOGLE_AUTH_BACKEND:        ``"google"`` or ``"console"``. ``"console"``
                                    accepts any string as a valid ID token — it is
                                    a complete authentication bypass and exists
                                    only so local development does not need a real
                                    Google project.

        DOCS_ENABLED:               Serve ``/docs``, ``/redoc`` and ``/openapi.json``.
        OTP_ECHO_IN_RESPONSE:       Return the OTP in the send-OTP response body.
        POOL_HEALTH_ENDPOINT_ENABLED: Serve ``/health/pool``.

        STORAGE_BACKEND:            ``"local"`` (filesystem, served at ``/uploads``)
                                    or ``"cloudinary"``.
        PUBLIC_BASE_URL:            Absolute origin clients use to reach this API.
                                    Used to build photo URLs; required when
                                    ``STORAGE_BACKEND=local``.
        UPLOADS_DIR:                Override the uploads directory. Empty means
                                    ``<repo root>/uploads``.

        RUN_BACKGROUND_JOBS:        Run the auto-escalation loop and pool monitor in
                                    this process. Must be true in exactly one
                                    process — they are not safe to run concurrently.
        LOG_LEVEL / LOG_TO_FILE / LOG_DIR / LOG_FORMAT: Logging configuration.

        DEV_MODE:                   Deprecated alias — see the module docstring.
    """

    # ── Environment ───────────────────────────────────────────────────────────
    ENVIRONMENT: Environment = "development"
    APP_VERSION: str = "1.0.0"

    # IANA timezone the deployment operates in. All timestamps are stored and
    # compared in UTC; this is only for wall-clock values a human entered, such
    # as WorkerShift.start_time/end_time ("09:00"), which are local times.
    # Comparing those against UTC marked every shift worker off-duty all day.
    LOCAL_TIMEZONE: str = "Asia/Kolkata"

    # ── Core ──────────────────────────────────────────────────────────────────
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Integration backends ──────────────────────────────────────────────────
    # "console" logs what would have been sent instead of calling the provider.
    SMS_BACKEND: Literal["msg91", "console"] = "msg91"
    EMAIL_BACKEND: Literal["smtp", "console"] = "smtp"
    PUSH_BACKEND: Literal["fcm", "console"] = "fcm"
    AADHAAR_BACKEND: Literal["surepass", "idfy", "console"] = "surepass"
    GOOGLE_AUTH_BACKEND: Literal["google", "console"] = "google"

    # Optional integrations — only validated in production when switched on.
    GOOGLE_AUTH_ENABLED: bool = False
    AADHAAR_ENABLED: bool = False

    # ── Exposure switches ─────────────────────────────────────────────────────
    # None means "not set" — resolved from ENVIRONMENT in _resolve_defaults.
    # This must stay distinguishable from an explicit False, otherwise a
    # production deployment that deliberately enables docs is indistinguishable
    # from one that simply forgot to disable them.
    DOCS_ENABLED: Optional[bool] = None
    OTP_ECHO_IN_RESPONSE: Optional[bool] = None
    POOL_HEALTH_ENDPOINT_ENABLED: Optional[bool] = None

    # ── Storage ───────────────────────────────────────────────────────────────
    STORAGE_BACKEND: Literal["local", "cloudinary"] = "local"
    PUBLIC_BASE_URL: str = ""
    UPLOADS_DIR: str = ""

    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Maximum photos attachable to one issue. Was a broken inline expression in
    # issues.py that always evaluated to 10 and referenced an undefined constant.
    MAX_PHOTOS_PER_ISSUE: int = 10

    # Hard cap on how many people one geofence broadcast can reach. A single
    # admin request must not be able to fan out without limit.
    GEOFENCE_NOTIFY_MAX_RECIPIENTS: int = 5000

    # How long an issue may sit in `assigned` with the worker never starting it
    # before the jobs service returns it to the open pool.
    ASSIGNMENT_TIMEOUT_MINUTES: int = 60

    # ── AI (optional; features degrade to no-ops without keys) ────────────────
    GROQ_API_KEY: str = ""       # chat replies + the scoped data-lookup agent
    GEMINI_API_KEY: str = ""     # photo classification + resolution verification
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Ceilings on every outbound model call. Without a timeout a hung provider
    # holds a worker thread until the client gives up; without a token cap a
    # single reply can run up an unbounded bill.
    # Whisper audio transcription. Read via a raw os.environ.get() in
    # issues.py, so it was the one config value in the app that bypassed the
    # whole Settings layer — absent from .env.example and invisible to
    # validate_settings, meaning nothing told you the feature was switched off.
    OPENAI_API_KEY: str = ""
    AI_REQUEST_TIMEOUT_SECONDS: float = 30.0
    AI_MAX_TOKENS: int = 1024

    # ── SMS — MSG91 ───────────────────────────────────────────────────────────
    MSG91_API_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""

    # ── Push — Firebase FCM ───────────────────────────────────────────────────
    FIREBASE_CREDENTIALS_PATH: str = "credentials/firebase-adminsdk.json"

    # ── Email — SMTP ──────────────────────────────────────────────────────────
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@civicapp.in"
    SMTP_TLS: bool = True

    # ── App URLs (used in worker invitation emails) ───────────────────────────
    APP_DOWNLOAD_URL: str = "https://civicapp.in/download"
    APP_HELP_URL: str = "https://civicapp.in/help"
    SUPPORT_EMAIL: str = "support@civicapp.in"

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""

    # ── Aadhaar KYC provider ──────────────────────────────────────────────────
    AADHAR_KYC_PROVIDER: str = "surepass"
    AADHAR_KYC_API_KEY: str = ""
    AADHAR_KYC_URL: str = ""
    AADHAR_KYC_ACCOUNT_ID: str = ""

    # ── HTTP ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001"
    RATE_LIMIT_PER_MINUTE: int = 60
    MAX_BODY_SIZE: int = 10 * 1024 * 1024

    # ── Operations ────────────────────────────────────────────────────────────
    RUN_BACKGROUND_JOBS: bool = True
    LOG_LEVEL: str = ""
    LOG_TO_FILE: Optional[bool] = None
    LOG_DIR: str = "logs"
    LOG_FORMAT: str = ""
    SENTRY_DSN: str = ""
    # Deliberate opt-out of the production SENTRY_DSN requirement. Named so that
    # running without error reporting is a decision someone wrote down, not an
    # oversight — which is what it was when this was only a warning.
    SENTRY_DSN_OPTIONAL: bool = False

    # Required in production to call POST /setup/admin while no admin exists.
    SETUP_TOKEN: str = ""

    # ── Deprecated ────────────────────────────────────────────────────────────
    DEV_MODE: bool = False

    # extra="ignore": the .env file is shared with Docker Compose, which reads
    # its own keys from it (POSTGRES_*, PORT, UID, ...). Those are not app
    # settings, and pydantic-settings rejects unknown keys read from a dotenv
    # file by default — which breaks any container that can see .env.
    model_config = {"env_file": ".env", "extra": "ignore"}

    # ── Field coercion ────────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, values):
        """Drop empty values so a blank env var means "not set", not "invalid".

        ``LOG_TO_FILE=`` or ``SMS_BACKEND=`` in a .env file is the natural way
        to write "use the default", but pydantic would reject the empty string
        as an invalid bool / an invalid Literal. Removing the key entirely —
        rather than coercing it to the default — also keeps it out of
        ``model_fields_set``, which is what :meth:`_resolve_defaults` uses to
        tell an explicit choice from an unset one.
        """
        if isinstance(values, dict):
            return {
                k: v
                for k, v in values.items()
                if not (isinstance(v, str) and not v.strip())
            }
        return values

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def cors_origin_list(self) -> List[str]:
        """CORS origins as a list. ``["*"]`` means allow any origin."""
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def uploads_path(self) -> Path:
        """Absolute path to the local uploads directory."""
        if self.UPLOADS_DIR:
            return Path(self.UPLOADS_DIR).resolve()
        # app/core/config.py -> app/core -> app -> repo root
        return Path(__file__).resolve().parents[2] / "uploads"

    @property
    def mocked_backends(self) -> List[str]:
        """Names of integrations currently running against the console mock."""
        pairs = (
            ("SMS_BACKEND", self.SMS_BACKEND),
            ("EMAIL_BACKEND", self.EMAIL_BACKEND),
            ("PUSH_BACKEND", self.PUSH_BACKEND),
            ("AADHAAR_BACKEND", self.AADHAAR_BACKEND),
            ("GOOGLE_AUTH_BACKEND", self.GOOGLE_AUTH_BACKEND),
        )
        return [name for name, value in pairs if value == "console"]

    # ── Defaulting ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _resolve_defaults(self) -> "Settings":
        """Resolve unset switches from ENVIRONMENT and apply the DEV_MODE alias.

        Runs after field validation, so ``ENVIRONMENT`` is already known. Unset
        switches (``None``) become permissive in development and restrictive in
        production. ``DEV_MODE`` then layers the legacy behaviour on top, but
        only in development — in production it is rejected outright by
        :func:`validate_settings`.
        """
        dev = not self.is_production

        if self.DOCS_ENABLED is None:
            self.DOCS_ENABLED = dev
        if self.OTP_ECHO_IN_RESPONSE is None:
            self.OTP_ECHO_IN_RESPONSE = dev
        if self.POOL_HEALTH_ENDPOINT_ENABLED is None:
            self.POOL_HEALTH_ENDPOINT_ENABLED = dev
        if self.LOG_TO_FILE is None:
            self.LOG_TO_FILE = dev
        if not self.LOG_LEVEL:
            self.LOG_LEVEL = "DEBUG" if dev else "INFO"
        if not self.LOG_FORMAT:
            self.LOG_FORMAT = "text" if dev else "json"
        self.LOG_LEVEL = self.LOG_LEVEL.upper()

        # Deprecated DEV_MODE alias. Only honoured in development; production
        # rejects it in validate_settings() rather than silently obeying it.
        if self.DEV_MODE and dev:
            warnings.warn(
                "DEV_MODE is deprecated. It now only sets defaults for other "
                "settings. Replace it with the specific switches you need: "
                "SMS_BACKEND / EMAIL_BACKEND / PUSH_BACKEND / AADHAAR_BACKEND / "
                "GOOGLE_AUTH_BACKEND = console, and DOCS_ENABLED / "
                "OTP_ECHO_IN_RESPONSE / POOL_HEALTH_ENDPOINT_ENABLED = true.",
                DeprecationWarning,
                stacklevel=2,
            )
            # Only override backends still sitting at their default, so an
            # explicit setting always wins over the legacy flag.
            fields_set = self.model_fields_set
            if "SMS_BACKEND" not in fields_set:
                self.SMS_BACKEND = "console"
            if "EMAIL_BACKEND" not in fields_set:
                self.EMAIL_BACKEND = "console"
            if "PUSH_BACKEND" not in fields_set:
                self.PUSH_BACKEND = "console"
            if "AADHAAR_BACKEND" not in fields_set:
                self.AADHAAR_BACKEND = "console"
            if "GOOGLE_AUTH_BACKEND" not in fields_set:
                self.GOOGLE_AUTH_BACKEND = "console"

        return self


settings = Settings()


# ── Validation ────────────────────────────────────────────────────────────────


def _is_absolute_url(value: str, *, require_https: bool = False) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    if require_https and parsed.scheme != "https":
        return False
    return parsed.scheme in {"http", "https"}


def collect_problems(s: "Settings", *, runtime: bool = True) -> List[str]:
    """Return every configuration problem found, as human-readable strings.

    Args:
        s:       Settings instance to check.
        runtime: False when running a non-serving command such as an Alembic
                 migration. Those processes never send an SMS or serve a photo,
                 so requiring MSG91/SMTP/storage credentials there would block
                 migrations for no reason. Checks that matter to any process
                 (secret strength, database URL) still run.

    Returns:
        A list of problems. Empty means the configuration is sound.
    """
    problems: List[str] = []

    # ── Always checked, in every environment and every process ───────────────
    if not s.DATABASE_URL:
        problems.append("DATABASE_URL is not set")
    elif not s.DATABASE_URL.startswith(("postgresql://", "postgresql+")):
        problems.append(
            f"DATABASE_URL must be a postgresql:// URL (got {s.DATABASE_URL.split('://')[0]}://)"
        )

    if s.ALGORITHM not in {"HS256", "HS384", "HS512"}:
        problems.append(
            f"ALGORITHM must be one of HS256/HS384/HS512 (got {s.ALGORITHM!r})"
        )

    # Everything below is a production requirement. In development these are
    # reported as warnings so the same list is visible without blocking work.
    if not s.is_production:
        return problems

    # ── Secrets ──────────────────────────────────────────────────────────────
    if not s.SECRET_KEY:
        problems.append("SECRET_KEY is not set")
    else:
        if len(s.SECRET_KEY) < 32:
            problems.append(
                f"SECRET_KEY must be at least 32 characters (got {len(s.SECRET_KEY)})"
            )
        if s.SECRET_KEY.strip().lower() in _PLACEHOLDER_SECRETS:
            problems.append("SECRET_KEY is a placeholder value from .env.example")
        if len(set(s.SECRET_KEY)) < 16:
            problems.append(
                "SECRET_KEY has too little variety to be random "
                f"({len(set(s.SECRET_KEY))} distinct characters)"
            )

    # ── Mocks and exposure ───────────────────────────────────────────────────
    if s.DEV_MODE:
        problems.append("DEV_MODE must be false in production")

    for backend in s.mocked_backends:
        problems.append(
            f"{backend} is 'console' — that is a mock and must not run in production"
        )

    if s.OTP_ECHO_IN_RESPONSE:
        problems.append(
            "OTP_ECHO_IN_RESPONSE must be false in production — it returns the "
            "login OTP in the API response body"
        )
    if s.POOL_HEALTH_ENDPOINT_ENABLED:
        problems.append("POOL_HEALTH_ENDPOINT_ENABLED must be false in production")
    # A production deploy with no error reporting is how a job that threw a
    # TypeError on every cycle went unnoticed for an hour. This was a warning;
    # warnings do not block startup, and nobody reads one line in a boot log.
    if not s.SENTRY_DSN and not s.SENTRY_DSN_OPTIONAL:
        problems.append(
            "SENTRY_DSN is not set — a production deployment with no error "
            "reporting has no way to tell you when something breaks. Set it, or "
            "set SENTRY_DSN_OPTIONAL=true to acknowledge running blind."
        )

    # The DSN's password. `docker compose up` with no .env silently gets
    # `civic` from the `${POSTGRES_PASSWORD:-civic}` fallback — SECRET_KEY was
    # checked for placeholders since Phase 2, this was not checked at all.
    db_password = urlparse(s.DATABASE_URL).password or ""
    if db_password.lower() in _WEAK_DB_PASSWORDS:
        problems.append(
            f"DATABASE_URL uses the weak default password {db_password!r} — "
            "set POSTGRES_PASSWORD to a generated value"
        )
    elif db_password and len(db_password) < 12:
        problems.append(
            f"DATABASE_URL password is only {len(db_password)} characters; use at least 12"
        )

    if s.LOG_LEVEL == "DEBUG":
        problems.append(
            "LOG_LEVEL must not be DEBUG in production — debug records include "
            "OTPs and other credentials"
        )

    # ── CORS ─────────────────────────────────────────────────────────────────
    origins = s.cors_origin_list
    if origins == ["*"]:
        problems.append("CORS_ORIGINS must not be '*' in production")
    elif not origins:
        problems.append("CORS_ORIGINS is empty")
    else:
        for origin in origins:
            if not _is_absolute_url(origin):
                problems.append(f"CORS_ORIGINS entry is not an absolute URL: {origin!r}")
            elif not origin.startswith("https://"):
                problems.append(f"CORS_ORIGINS entry must use https: {origin!r}")

    if s.ACCESS_TOKEN_EXPIRE_MINUTES > 60:
        problems.append(
            f"ACCESS_TOKEN_EXPIRE_MINUTES is {s.ACCESS_TOKEN_EXPIRE_MINUTES}; "
            "60 or less is expected in production"
        )
    if s.RATE_LIMIT_PER_MINUTE <= 0:
        problems.append("RATE_LIMIT_PER_MINUTE must be greater than 0")

    if not runtime:
        return problems

    # ── Credentials only a serving process needs ─────────────────────────────
    # SMS is first because it matters most: phone OTP is the only login path
    # that works for every role, and the bootstrap admin has no password. If
    # MSG91 is misconfigured, nobody can log in — including you.
    if s.SMS_BACKEND == "msg91":
        if not s.MSG91_API_KEY:
            problems.append(
                "MSG91_API_KEY is not set — no user could log in, since phone "
                "OTP is the only login path available to every role"
            )
        if not s.MSG91_TEMPLATE_ID:
            problems.append("MSG91_TEMPLATE_ID is not set")

    if s.EMAIL_BACKEND == "smtp":
        for field in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
            if not getattr(s, field):
                problems.append(
                    f"{field} is not set — worker invitations are the only way "
                    "to onboard a worker"
                )

    if s.PUSH_BACKEND == "fcm" and not Path(s.FIREBASE_CREDENTIALS_PATH).exists():
        problems.append(
            f"FIREBASE_CREDENTIALS_PATH does not exist: {s.FIREBASE_CREDENTIALS_PATH}"
        )

    if s.GOOGLE_AUTH_ENABLED and s.GOOGLE_AUTH_BACKEND == "google" and not s.GOOGLE_CLIENT_ID:
        problems.append("GOOGLE_AUTH_ENABLED is true but GOOGLE_CLIENT_ID is not set")

    if s.AADHAAR_ENABLED and s.AADHAAR_BACKEND != "console":
        if not s.AADHAR_KYC_API_KEY:
            problems.append("AADHAAR_ENABLED is true but AADHAR_KYC_API_KEY is not set")
        if not s.AADHAR_KYC_URL:
            problems.append("AADHAAR_ENABLED is true but AADHAR_KYC_URL is not set")
        elif not _is_absolute_url(s.AADHAR_KYC_URL, require_https=True):
            problems.append(
                f"AADHAR_KYC_URL must be an absolute https URL (got {s.AADHAR_KYC_URL!r})"
            )

    # ── Storage ──────────────────────────────────────────────────────────────
    if s.STORAGE_BACKEND == "local":
        if not s.PUBLIC_BASE_URL:
            problems.append(
                "PUBLIC_BASE_URL is required when STORAGE_BACKEND=local — photo "
                "URLs are built from it"
            )
        elif not _is_absolute_url(s.PUBLIC_BASE_URL, require_https=True):
            problems.append(
                f"PUBLIC_BASE_URL must be an absolute https URL (got {s.PUBLIC_BASE_URL!r})"
            )
    elif s.STORAGE_BACKEND == "cloudinary":
        for field in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
            if not getattr(s, field):
                problems.append(f"{field} is not set but STORAGE_BACKEND=cloudinary")

    return problems


def collect_warnings(s: "Settings") -> List[str]:
    """Return advisory notes that never block startup."""
    notes: List[str] = []
    if s.is_production:
        # A warning, not a refusal: publishing the schema is a defensible
        # choice for a public API, but it should be a decision, not an oversight.
        if s.DOCS_ENABLED:
            notes.append(
                "DOCS_ENABLED is true in production — /docs, /redoc and the full "
                "OpenAPI schema are publicly readable"
            )

        # Not fatal: Postgres reached over a private network or a unix socket
        # sidecar is a legitimate deployment and needs no TLS on the wire.
        if "sslmode=" not in s.DATABASE_URL:
            notes.append(
                "DATABASE_URL has no sslmode — fine on a private network, but "
                "set sslmode=require if the database is reached over the internet"
            )
        if not s.SETUP_TOKEN:
            notes.append(
                "SETUP_TOKEN is not set — POST /setup/admin is unauthenticated "
                "while no admin account exists"
            )

    # The AI keys were validated nowhere at all, so a production deploy with
    # both empty started perfectly clean while three advertised features were
    # silently dead: every photo recorded a null classification, every worker
    # resolution skipped the AI quality gate, and /chat answered "unavailable".
    # Warnings rather than failures — running without AI is a legitimate choice.
    if not s.GROQ_API_KEY:
        notes.append(
            "GROQ_API_KEY is not set — /chat returns a fixed unavailable message "
            "and the data-lookup agent will not run"
        )
    if not s.GEMINI_API_KEY:
        notes.append(
            "GEMINI_API_KEY is not set — photo issue-classification and "
            "resolution verification return empty results"
        )
    return notes


def validate_settings(s: Optional["Settings"] = None, *, runtime: bool = True) -> List[str]:
    """Validate the configuration; refuse to start in production if it is unsafe.

    Every problem is collected and reported together rather than raising on the
    first one, so a broken production configuration can be fixed in a single
    pass instead of one redeploy per mistake.

    Args:
        s:       Settings to validate. Defaults to the module-level instance.
        runtime: See :func:`collect_problems`.

    Returns:
        The list of problems found (empty in production, since a non-empty list
        raises).

    Raises:
        ConfigurationError: In production, when any problem was found.
    """
    from app.core.logger import get_logger

    s = s or settings
    logger = get_logger("config")

    problems = collect_problems(s, runtime=runtime)

    for note in collect_warnings(s):
        logger.warning("config: %s", note)

    if not problems:
        logger.info(
            "Configuration validated (environment=%s, storage=%s, mocked=%s)",
            s.ENVIRONMENT,
            s.STORAGE_BACKEND,
            ", ".join(s.mocked_backends) or "none",
        )
        return problems

    if s.is_production:
        raise ConfigurationError(
            "Refusing to start in production — fix the following:\n  - "
            + "\n  - ".join(problems)
        )

    for problem in problems:
        logger.warning("config: %s", problem)
    logger.warning(
        "config: %d problem(s) above would prevent startup with ENVIRONMENT=production",
        len(problems),
    )
    return problems
