# Running the whole stack locally

Three surfaces: FastAPI backend, Next.js admin console, Expo mobile app.

**The short version: you need no API keys at all to run and test everything
except AI features and real SMS/email delivery.** Every external integration has
a console backend that prints to stdout instead of calling out. That is what
`ENVIRONMENT=development` selects.

---

## 1. Backend

```bash
cd civic-backend
cp .env.example .env          # then edit — see below
docker compose up -d db
docker compose up -d          # migrate runs first, then api + jobs
```

Check it came up:

```bash
curl -s localhost:8000/health/live          # 200
docker compose ps                           # api, db, jobs all "Up"
```

### The only two variables you must set

| Variable | Why |
|---|---|
| `SECRET_KEY` | Signs JWTs. Startup **refuses** a placeholder value. Generate one: `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | Defaults to `civic` if unset, which is fine locally but is checked in production |

`DATABASE_URL` is already wired for the compose network — leave it.

### Seeding

**Seed the location hierarchy first.** A fresh database has no districts,
talukas or wards. Citizens can still report (`ward_id` is optional), but every
scoped admin role depends on it — a `ward_admin` with a NULL `ward_id` is
refused by `get_admin_scope_filter` rather than silently widened, so without
this they see nothing:

```bash
docker compose run --rm api python scripts/seed_locations.py
```

Idempotent — safe to re-run. Gujarat data ships in
`scripts/locations_gujarat.csv`; pass `--file` for your own.

### A first admin

There is no bootstrap script, so mint one directly:

```bash
docker compose exec -T api python - <<'PY'
from app.database import SessionLocal
from app.models.user import User
import uuid
db = SessionLocal()
u = User(id=uuid.uuid4(), phone="+919900000001", name="Local Admin",
         role="admin", is_active=True, language="en")
db.add(u); db.commit()
print("admin:", u.phone)
PY
```

### Signing in without SMS

`SMS_BACKEND=console` means OTPs are **printed to the logs**, not sent:

```bash
curl -s -X POST localhost:8000/auth/send-otp \
  -H 'Content-Type: application/json' -d '{"phone":"+919900000001"}'

docker compose logs api --tail=30 | grep -i otp     # the code is here
```

In development `OTP_ECHO_IN_RESPONSE` may also return it in the response body.

---

## 2. Admin console

```bash
cd civic-frontend/admin
npm install
echo 'NEXT_PUBLIC_API_URL=http://localhost:8000' > .env.local
npm run dev            # http://localhost:3000
```

Sign in with the admin phone above and the OTP from the backend logs.

### Optional

| Variable | Without it |
|---|---|
| `NEXT_PUBLIC_GOOGLE_MAPS_ID` + a Maps JS API key | Map panes render grey. Everything else works. Affects `/dashboard/map`, the issue detail modal, geofence and bulk-notification pickers |

> **Build note:** `next/font` downloads Inter and IBM Plex Mono at build time, so
> the first `npm run build` needs network access. If you build offline it will
> fail there — swap to `next/font/local` with the woff2 files in `public/`.

---

## 3. Mobile app

```bash
cd civic-frontend/mobile
npm install
npx expo start
```

Scan the QR with **Expo Go**.

**`localhost` on a phone means the phone.** Point it at your machine's LAN
address:

```bash
EXPO_PUBLIC_API_URL=http://192.168.1.x:8000 npx expo start
```

Find yours with `hostname -I | awk '{print $1}'`, and make sure
`CORS_ORIGINS` in the backend `.env` includes it.

There is a written test plan at `mobile/DEVICE-CHECKLIST.md` — 40 checks
covering the things that cannot be verified without real hardware (haptics,
safe-area insets, keyboard behaviour).

---

## 4. What each API key actually unlocks

Everything below is **optional**. The app runs, and every screen loads, without
any of them.

| Key | Unlocks | Without it |
|---|---|---|
| `GROQ_API_KEY` | `POST /chat` replies, the scoped data-lookup agent, and the written summary on `/dashboard/ai-insights` | Chat returns a fixed "unavailable" message. Insights returns **all its numbers** and `narrative: null` — deliberately, so a provider outage never blanks the dashboard |
| `GEMINI_API_KEY` | Photo classification on report, and after-photo resolution verification | Those fields stay null. Issues still file and resolve normally |
| `OPENAI_API_KEY` | Voice-note transcription on the report screen | Transcription returns empty |
| `CLOUDINARY_*` (3 vars) | Cloud photo storage | Photos are stored on local disk under `UPLOADS_DIR`. Fine for local |
| `SMTP_*` (4 vars) | Real worker-invitation emails | `EMAIL_BACKEND=console` prints the email to the logs, invite link included |
| `FIREBASE_CREDENTIALS_PATH` | Real push notifications | `PUSH_BACKEND=console` logs them. In-app notification rows are still created, so the bell badge works |
| `GOOGLE_CLIENT_ID` | Google sign-in on mobile | Button is inert; OTP and password sign-in both work |
| `AADHAR_KYC_*` | Real Aadhaar verification | `AADHAAR_BACKEND=console` accepts any well-formed number |
| `SENTRY_DSN` | Error reporting | Errors go to the console. Both apps now have a structured logger with a one-line sink swap — see `src/lib/logger.js` |
| Google Maps JS key | Map panes in the admin console | Grey boxes |

**Startup tells you.** `validate_settings` prints a warning line at boot naming
every integration running in mocked mode, so you never have to guess whether a
key took effect:

```
Mocked integrations active: SMS_BACKEND, EMAIL_BACKEND, PUSH_BACKEND,
AADHAAR_BACKEND, GOOGLE_AUTH_BACKEND — nothing is actually sent
```

---

## 5. Running the tests

```bash
# Backend — 219 tests, against a real throwaway Postgres
cd civic-backend
docker compose --profile test run --rm tests pytest -q

# Migrations are verified separately: the suite builds its schema with
# create_all and never exercises alembic, so a correct model with a missing
# migration passes the suite and fails a deploy.
docker compose run --rm --no-deps migrate alembic upgrade head
docker compose run --rm --no-deps migrate alembic check

# Admin — 128 tests
cd civic-frontend/admin && npx jest

# Shared types drift check — the frontend's equivalent of `alembic check`.
# Needs the backend running.
cd civic-frontend/packages/api-types && node scripts/generate.mjs --check
```

---

## 6. Gotchas that cost time

**Compose services run baked images, not your working tree.** After editing
backend source, `docker compose restart` does nothing — it re-runs the old
image. You need:

```bash
docker compose build api jobs migrate && docker compose up -d
```

The `tests` service is the exception; it bind-mounts `app/` and `tests/` so the
test loop reflects your edits immediately. That is deliberate — without it a
stale image made the suite pass against code that no longer existed.

**The `jobs` container reports no health status.** It serves no HTTP, so the
image's healthcheck is disabled for it. `Up` without `(healthy)` is correct.

**Ports.** Backend `8000` (`PORT`), admin `3000`, Postgres `5434`
(`DB_PORT`) — bound to `127.0.0.1` only, so it is never reachable from your
LAN. 5434 rather than 5432 to avoid colliding with a Postgres you may already
have running.

**A stale test database.** The suite drops and recreates its schema each run, so
a model change is picked up automatically. It refuses to do that against any
database whose name does not contain "test".
