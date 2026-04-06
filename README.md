# Civic Backend

Production-ready FastAPI backend for the **Civic Issue Reporting** platform — a system where citizens report municipal issues (potholes, garbage, streetlights, etc.), workers are auto-assigned and resolve them, and admins oversee the entire pipeline.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI (async) |
| Database | PostgreSQL via Supabase |
| ORM | SQLAlchemy |
| Migrations | Alembic |
| Image Storage | Cloudinary |
| AI | Google Gemini 2.5 Flash (classify + verify resolution + chatbot) |
| SMS / OTP | MSG91 |
| Push Notifications | Firebase Cloud Messaging (FCM) |
| Auth | JWT (15 min) + DB-backed refresh tokens (30 days) |
| KYC | Aadhaar OTP via Surepass / IDfy |
| Containerization | Docker + Docker Compose |

---

## Project Structure

```
civic-backend/
├── app/
│   ├── core/
│   │   ├── config.py          # All env settings via pydantic-settings
│   │   ├── deps.py            # get_current_user, require_role(), admin scope helpers
│   │   ├── logger.py          # Rotating file + console logger
│   │   ├── prompts.py         # Gemini prompt templates
│   │   └── security.py        # JWT encode/decode
│   ├── models/
│   │   ├── user.py            # User (citizen / worker / admin hierarchy)
│   │   ├── issue.py           # Civic issue with AI fields, priority, upvotes
│   │   ├── issue_comment.py   # Issue comments / interim updates
│   │   ├── issue_vote.py      # Issue upvotes (unique per user/issue)
│   │   ├── issue_flag.py      # Issue/comment reports for moderation
│   │   ├── announcement.py    # Admin broadcasts (scoped to ward/taluka/district/state)
│   │   ├── location.py        # District → Taluka → Ward hierarchy
│   │   ├── reward.py          # RewardTransaction + UserBadge
│   │   ├── ward_subscription.py # Citizen ward subscriptions for notifications
│   │   ├── worker_shift.py    # Worker weekly shift schedules
│   │   ├── notification.py    # In-app notifications
│   │   ├── otp.py             # OTP codes (phone + Aadhaar)
│   │   └── refresh_token.py   # DB-backed refresh tokens (revocable)
│   ├── routes/
│   │   ├── setup.py           # First-time admin bootstrap (locked after first admin)
│   │   ├── auth.py            # Phone OTP, Google OAuth, Aadhaar KYC, refresh, logout, phone change
│   │   ├── issues.py          # Issue CRUD, photos, comments, timeline, reopen
│   │   ├── workers.py         # Task accept/reject/resolve/block, location, leaderboard
│   │   ├── admin.py           # Dashboard, analytics, worker/citizen/sub-admin management, export
│   │   ├── features.py        # Upvotes, flags, search, shifts, ward subscriptions, announcements
│   │   ├── locations.py       # District/Taluka/Ward CRUD and tree
│   │   ├── rewards.py         # Points, badges, leaderboard
│   │   ├── notifications.py   # Read, delete notifications
│   │   └── chat.py            # Multilingual AI civic assistant
│   ├── schemas/               # Pydantic v2 request/response models
│   ├── services/
│   │   ├── ai_service.py      # Gemini: classify, verify resolution, chatbot, duplicate check
│   │   ├── aadhar_service.py  # Aadhaar OTP (Surepass + IDfy providers)
│   │   ├── auth_service.py    # OTP generation (CSPRNG), refresh token lifecycle
│   │   ├── geo_service.py     # Haversine auto-assign nearest online worker (shift-aware)
│   │   ├── google_auth.py     # Google ID token verification
│   │   ├── notification_service.py  # FCM push + in-app (en/hi/gu) + ward subscriptions
│   │   ├── rewards_service.py # Points ledger, badge unlock checks, leaderboard
│   │   ├── sms_service.py     # MSG91 OTP delivery
│   │   └── storage.py         # Cloudinary upload / local dev fallback
│   ├── database.py            # SQLAlchemy session + connection check
│   └── main.py                # App factory, CORS, middleware, auto-escalation background task
├── alembic/                   # Database migrations
├── credentials/               # Firebase service account JSON (git-ignored)
├── Dockerfile                 # Multi-stage production image
├── docker-compose.yml         # Dev with hot-reload
├── .dockerignore
├── create_admin.py            # CLI script to seed the first admin
├── run.py                     # Dev runner (auto free-port + .env reload)
├── requirements.txt
├── .env.example
└── test_api.py                # End-to-end integration test suite
```

---

## Quick Start

### Option A — Local (venv)

```bash
# 1. Clone and create virtualenv
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Fill in your values (see Environment Variables below)

# 4. Run database migrations
alembic upgrade head

# 5a. Start dev server (auto-picks a free port, reloads on .py + .env changes)
python run.py

# 5b. Or specify a port
python run.py 8080
```

### Option B — Docker

```bash
# Build and start (auto-reload enabled via volume mount)
docker compose up --build

# Custom port
PORT=9000 docker compose up

# Production build (no reload, 2 workers)
docker build -t civic-api .
docker run --env-file .env -p 8000:8000 civic-api
```

Server starts at `http://0.0.0.0:<port>`
Interactive docs at `http://localhost:<port>/docs`

### First-Time Setup (Bootstrap Admin)

After running migrations, no admin exists yet. Bootstrap the first admin using either method:

**Option 1 — API (recommended for mobile/web frontends):**
```bash
# 1. Check if setup is needed
GET /setup/status
# → {"setup_required": true}

# 2. Create the first admin
POST /setup/admin
{
  "phone": "+919876543210",
  "name": "Admin Name",
  "ward": "Ward 5",       # optional
  "language": "en"        # en | hi | gu
}
# → 201 with user object; endpoint is PERMANENTLY LOCKED after this call

# 3. Log in via OTP
POST /auth/send-otp  →  POST /auth/verify-otp  →  JWT tokens
```

**Option 2 — CLI script:**
```bash
python create_admin.py
# Prompts for phone, name, ward; checks existing accounts; offers promote-to-admin option
```

Once any admin account exists, `POST /setup/admin` returns `403 Setup already completed` permanently.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```env
# Database (Supabase PostgreSQL)
DATABASE_URL=postgresql://user:pass@host:5432/postgres?sslmode=require

# JWT
SECRET_KEY=your-secret-min-32-chars
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15

# Storage (Cloudinary)
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_API_KEY=
CLOUDINARY_API_SECRET=

# AI (Google Gemini)
GEMINI_API_KEY=

# SMS OTP (MSG91)
MSG91_API_KEY=
MSG91_TEMPLATE_ID=

# Push Notifications (Firebase FCM)
FIREBASE_CREDENTIALS_PATH=credentials/firebase-adminsdk.json

# Google OAuth (mobile/web sends id_token, backend verifies)
GOOGLE_CLIENT_ID=

# Aadhaar KYC — leave blank to use DEV_MODE mock
AADHAR_KYC_PROVIDER=surepass       # or: idfy
AADHAR_KYC_API_KEY=
AADHAR_KYC_URL=https://kyc-api.surepass.io/api/v1
AADHAR_KYC_ACCOUNT_ID=             # IDfy only

# CORS — set your frontend URL(s) in production
# CORS_ORIGINS=https://civic.yourdomain.com,https://admin.yourdomain.com
CORS_ORIGINS=*

# Dev mode — mocks all external APIs (OTP printed to console, no SMS/FCM sent)
DEV_MODE=true
```

---

## API Reference (101 endpoints)

Base URL: `http://localhost:<port>`
All protected routes require: `Authorization: Bearer <access_token>`

### Setup (`/setup`) — 2 endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/setup/status` | — | Returns `{"setup_required": bool}` — call on app launch |
| POST | `/setup/admin` | — | Create first admin account (permanently locked once any admin exists) |

### Auth (`/auth`) — 13 endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/auth/send-otp` | — | Send OTP to phone (60 s rate limit) |
| POST | `/auth/verify-otp` | — | Verify OTP → `access_token` + `refresh_token` |
| POST | `/auth/google` | — | Login / register with Google ID token |
| POST | `/auth/aadhar/send-otp` | — | Send OTP to Aadhaar-linked mobile |
| POST | `/auth/aadhar/verify` | — | Verify Aadhaar OTP → tokens + `aadhar_verified=true` |
| POST | `/auth/refresh` | — | Rotate refresh token, returns new pair |
| POST | `/auth/logout` | Required | Revoke current refresh token |
| GET | `/auth/me` | Required | Current user profile |
| PUT | `/auth/profile` | Required | Update name, ward, language, FCM token, phone, email |
| GET | `/auth/providers` | Required | Linked auth methods (phone / Google / Aadhaar) |
| DELETE | `/auth/account` | Required | Soft-delete account + revoke all tokens |
| POST | `/auth/phone/change/send-otp` | Required | Send OTP to new phone number (pre-change verification) |
| POST | `/auth/phone/change/verify` | Required | Verify OTP and switch to the new phone number |

### Issues (`/issues`) — 16 endpoints

| Method | Path | Roles | Description |
|--------|------|-------|-------------|
| POST | `/issues` | citizen, admin | Create issue (auto-assigns nearest worker, duplicate check) |
| GET | `/issues` | all | List issues (citizens: own, workers: assigned, admin: all) |
| GET | `/issues/{id}` | all | Get single issue |
| PATCH | `/issues/{id}` | all | Update status / rating / notes (role-restricted fields) |
| GET | `/issues/nearby` | all | Issues within radius (km) of coordinates |
| GET | `/issues/ward-health` | all | 0–100 health score for a ward |
| POST | `/issues/{id}/photos` | all | Upload before (citizen) / after (worker, admin) photos |
| DELETE | `/issues/{id}/photos` | all | Remove a specific photo URL |
| POST | `/issues/{id}/reopen` | citizen, admin | Reopen a resolved issue |
| GET | `/issues/{id}/timeline` | all | Chronological status event list |
| POST | `/issues/{id}/comments` | all | Post a comment / interim update |
| GET | `/issues/{id}/comments` | all | List all comments |
| POST | `/issues/{id}/upvote` | all | Upvote / endorse an issue (once per user) |
| DELETE | `/issues/{id}/upvote` | all | Remove your upvote |
| POST | `/issues/{id}/flag` | all | Flag an issue or comment for admin review |
| GET | `/issues/search` | all | Full-text keyword search across description and address |

### Workers (`/workers`) — 14 endpoints

| Method | Path | Roles | Description |
|--------|------|-------|-------------|
| GET | `/workers/tasks` | worker | Active assigned tasks (sorted by severity) |
| POST | `/workers/tasks/{id}/accept` | worker | Accept task → `in_progress` |
| POST | `/workers/tasks/{id}/reject` | worker | Reject task → auto-reassigned to next nearest worker |
| POST | `/workers/tasks/{id}/resolve` | worker | Resolve with after-photo + AI verification |
| POST | `/workers/tasks/{id}/block` | worker | Flag task as blocked (equipment / dept / access) |
| GET | `/workers/tasks/history` | worker | Resolved / closed task history (paginated) |
| PUT | `/workers/status` | worker, admin | Toggle online / offline |
| PUT | `/workers/location` | worker | Update live GPS location |
| GET | `/workers/stats` | worker | Today's task counts and avg rating |
| GET | `/workers/leaderboard` | worker | Leaderboard with current worker's rank highlighted |
| PUT | `/workers/availability` | worker | Toggle accepting new task assignments (separate from online/offline) |
| GET | `/workers/shifts` | worker | List my weekly shift schedule |
| POST | `/workers/shifts` | worker | Create or update a shift for a day (upsert) |
| DELETE | `/workers/shifts/{day}` | worker | Remove shift for a specific day (0=Monday … 6=Sunday) |

### Admin (`/admin`) — 31 endpoints

All admin endpoints require an admin role. Endpoints are automatically scoped to the admin's geographic area (ward / taluka / district).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/dashboard` | Live stats (open, in-progress, resolved today, avg resolution hours) |
| GET | `/admin/analytics` | Trend data (daily, by type, by status, by priority, top wards) |
| GET | `/admin/heatmap` | Lat/lng/weight points for map overlay |
| GET | `/admin/issues` | All issues with filters + pagination (scoped) |
| GET | `/admin/issues/export` | Download filtered issues as CSV |
| POST | `/admin/issues/{id}/assign` | Manually assign a worker |
| POST | `/admin/issues/{id}/reassign` | Reassign to a different worker |
| POST | `/admin/issues/{id}/escalate` | Manually escalate an issue |
| POST | `/admin/issues/bulk` | Bulk close / escalate / assign / set_priority |
| DELETE | `/admin/issues/{id}` | Soft-delete an issue |
| GET | `/admin/workers` | Paginated workers with search + filters |
| POST | `/admin/workers` | Create worker account |
| GET | `/admin/workers/{id}` | Worker profile + task statistics |
| PUT | `/admin/workers/{id}` | Update worker profile |
| POST | `/admin/workers/{id}/deactivate` | Deactivate worker |
| POST | `/admin/workers/{id}/reactivate` | Reactivate worker |
| GET | `/admin/workers/leaderboard` | Worker performance ranking |
| GET | `/admin/workers/locations` | Live GPS of all workers (admin map) |
| GET | `/admin/workers/{id}/location` | Last GPS of a specific worker |
| GET | `/admin/citizens` | Paginated citizen list with search + issue count |
| GET | `/admin/citizens/{id}` | Citizen profile + issue statistics |
| POST | `/admin/citizens/{id}/deactivate` | Deactivate citizen (abuse moderation) |
| POST | `/admin/citizens/{id}/reactivate` | Reactivate citizen |
| POST | `/admin/admins` | Create sub-admin (ward_admin / taluka_admin / district_admin) |
| GET | `/admin/admins` | List sub-admins in scope (filterable by role) |
| PATCH | `/admin/users/{id}/role` | Change a user's role (super-admin only) |
| POST | `/admin/announcements` | Broadcast announcement (ward / taluka / district / state) |
| GET | `/admin/announcements` | List announcements in scope |
| DELETE | `/admin/announcements/{id}` | Delete an announcement |
| GET | `/admin/flags` | List content flags/reports for moderation |
| PATCH | `/admin/flags/{id}` | Mark a flag as reviewed or dismissed |

### Me / User (`/me`) — 12 endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/me/notifications` | Notifications (`?unread_only=true`, paginated) |
| POST | `/me/notifications/read-all` | Mark all as read |
| POST | `/me/notifications/{id}/read` | Mark one as read |
| DELETE | `/me/notifications/{id}` | Delete one notification |
| DELETE | `/me/notifications` | Clear all notifications |
| GET | `/me/rewards` | Points, level, rank, recent transactions, earned badges |
| GET | `/me/subscriptions` | List ward subscriptions (citizen only) |
| POST | `/me/subscriptions` | Subscribe to a ward for issue notifications (max 10) |
| DELETE | `/me/subscriptions/{ward_id}` | Unsubscribe from a ward |

### Rewards / Leaderboard — 3 endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/leaderboard/citizens` | Top citizens ranked by total reward points |
| GET | `/leaderboard/workers` | Top workers ranked by total reward points |
| GET | `/badges` | All available badges (earned + unearned, for achievement gallery) |

### Locations (`/locations`) — 10 endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/locations/districts` | — | List all districts (public) |
| POST | `/locations/districts` | super-admin | Create a district |
| DELETE | `/locations/districts/{id}` | super-admin | Delete a district (cascades to talukas → wards) |
| GET | `/locations/districts/{id}/talukas` | — | List talukas in a district (public) |
| POST | `/locations/districts/{id}/talukas` | district_admin+ | Create a taluka |
| DELETE | `/locations/talukas/{id}` | district_admin+ | Delete a taluka (cascades to wards) |
| GET | `/locations/talukas/{id}/wards` | — | List wards in a taluka (public) |
| POST | `/locations/talukas/{id}/wards` | taluka_admin+ | Create a ward |
| DELETE | `/locations/wards/{id}` | taluka_admin+ | Delete a ward |
| GET | `/locations/tree` | — | Full State → District → Taluka → Ward tree (public) |

### Announcements (`/announcements`) — 1 endpoint

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/announcements` | Required | List active announcements for given ward / taluka / district |

### Chat — 1 endpoint

| Method | Path | Description |
|--------|------|-------------|
| POST | `/chat` | Multilingual AI civic assistant (body: `{"message": "...", "issue_id": "..."}`) |

### System — 1 endpoint

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Health check with live DB connectivity status |

---

## Authentication Flow

```
Phone OTP:
  POST /auth/send-otp  →  OTP via SMS (MSG91)
  POST /auth/verify-otp  →  access_token (15 min) + refresh_token (30 days)

Google OAuth (mobile-first):
  Client sends Google id_token  →  POST /auth/google  →  tokens

Aadhaar KYC:
  POST /auth/aadhar/send-otp  →  OTP to Aadhaar-linked mobile
  POST /auth/aadhar/verify    →  tokens + aadhar_verified=true

Phone number change:
  POST /auth/phone/change/send-otp  →  OTP to the new phone
  POST /auth/phone/change/verify    →  phone updated, all tokens revoked (re-login required)

Token refresh (rotation):
  POST /auth/refresh  →  old token revoked, new pair issued

Logout:
  POST /auth/logout  →  refresh token revoked in DB
```

---

## Roles & Permissions

### User Roles

| Role | Can do |
|------|--------|
| **citizen** | Create/view own issues, upload before-photos, rate resolutions, reopen, comment on own issues, upvote, flag, ward subscriptions |
| **worker** | Accept/reject/resolve/block assigned tasks, upload after-photos, update location, manage shifts, comment on assigned issues |
| **ward_admin** | Admin panel scoped to their ward; can create workers for that ward |
| **taluka_admin** | Scoped to their taluka; can create ward_admins and workers |
| **district_admin** | Scoped to their district; can create taluka_admins and below |
| **admin** | Full access — manage all users, change roles, cross-district data, state-wide announcements |

Workers are created by admins only — they cannot self-register.
Sub-admins are created by super-admin or the admin level directly above.
Admins can change any user's role via `PATCH /admin/users/{id}/role`.

---

## AI Pipeline

| Feature | Trigger | Model | What it does |
|---|---|---|---|
| **Issue Classification** | Citizen uploads before-photo | Gemini 2.5 Flash | Detects `issue_type`, `severity`, `confidence`, tags, suggested description |
| **Resolution Verification** | Worker submits after-photo | Gemini 2.5 Flash | Checks if fix is `good`, `partial`, or `poor`; notifies admins on poor quality |
| **Duplicate Detection** | Every issue creation | Haversine (no API cost) | Flags same issue type within 50 m in last 48 h |
| **Multilingual Chatbot** | `POST /chat` | Gemini 2.5 Flash | Auto-detects en/hi/gu, responds in same language, uses issue context |

All AI features degrade gracefully — if `GEMINI_API_KEY` is missing, classification returns empty defaults and the system continues normally.

---

## Rewards & Gamification

Citizens and workers earn points for key actions. Points accumulate to unlock levels and badges.

### Point Events

| Event | Points | Who earns |
|---|---|---|
| `report_issue` | 10 | citizen (per report) |
| `issue_resolved` | 20 | citizen (when their issue is resolved) |
| `vote_received` | 5 | citizen (when their issue is upvoted) |
| `rate_issue` | 5 | citizen (for rating a resolved issue) |
| `aadhar_verified` | 50 | citizen (one-time, on Aadhaar KYC) |
| `first_report` | 25 | citizen (one-time bonus) |
| `resolve_issue` | 30 | worker (per resolution) |
| `five_star_rating` | 25 | worker (per 5-star citizen rating received) |
| `fast_resolve` | 15 | worker (resolved within 24 h) |
| `weekly_streak` | 20 | worker (zero rejections in a week) |
| `first_resolution` | 25 | worker (one-time bonus) |

### Levels

| Points | Level | Name |
|---|---|---|
| 0+ | 1 | Newcomer |
| 100+ | 2 | Active |
| 300+ | 3 | Contributor |
| 600+ | 4 | Champion |
| 1000+ | 5 | Hero |
| 2000+ | 6 | Legend |

### Badges (examples)

Citizens earn: **First Report**, **Active Reporter** (10 issues), **Super Reporter** (50 issues), **Verified Citizen**, **Community Voice** (50+ upvotes), **Engaged Citizen** (rated 10 issues).

Workers earn: **First Resolution**, **Dedicated Worker** (10 resolved), **Century Resolver** (100 resolved), **Fast Responder** (10 fast resolves), **Top Rated** (20 five-star ratings), **Streak Master** (4 consecutive no-rejection weeks).

---

## Key Features

- **Auto-assignment** — Shift-aware Haversine geo-routing assigns the nearest online+available worker; rejected issues reassign to the next nearest (excluding the rejector)
- **AI quality control** — Gemini Vision classifies issues from before-photo and verifies resolution from after-photo; poor resolution triggers admin notification
- **Duplicate detection** — Same issue type within 50 m flagged automatically; falls back to rule-based same-ward/same-type check
- **Auto-escalation** — Background task runs hourly; issues open > 48 h with no assignment are escalated and all admins notified
- **Multilingual notifications** — In-app + FCM push in English, Hindi, Gujarati based on user language preference
- **Ward subscriptions** — Citizens subscribe to up to 10 wards and receive FCM push when new issues are filed
- **Issue upvotes** — Citizens and workers endorse high-priority issues; sorted by upvote count in search results
- **Issue comments** — Workers post interim updates; citizens and admins follow progress
- **Worker shifts** — Workers configure weekly HH:MM shift windows; geo-routing prefers in-shift workers
- **Availability toggle** — Workers can pause assignments (break / lunch) without going offline
- **Rewards & badges** — Points ledger, level progression, badge unlocks, leaderboard for citizens and workers
- **Sub-admin hierarchy** — ward_admin → taluka_admin → district_admin → admin; each level scoped geographically
- **Announcements** — Admins broadcast notices scoped to ward / taluka / district / state with optional expiry
- **Content moderation** — Citizens flag issues/comments; admin reviews pending flags via `/admin/flags`
- **Bulk operations** — Admins close, escalate, assign, or reprioritize multiple issues in one call
- **DB-backed refresh tokens** — SHA-256 hashed, revocable per-token or all-at-once (logout / account delete)
- **Ward health score** — 0–100 per ward: 50% resolution rate + 30% speed + 20% citizen satisfaction
- **CSV export** — Admin exports filtered issues in one API call
- **Live DB health check** — `GET /` pings PostgreSQL and returns 503 if disconnected
- **CORS configurable** — `*` in dev, comma-separated origins in production

---

## Database Models

| Model | Table | Key fields |
|---|---|---|
| **User** | `users` | phone, email, google_id, aadhar_hash, role, ward, ward_id, taluka_id, district_id, department, language, lat/lng, fcm_token, is_online, is_available |
| **Issue** | `issues` | reporter_id, assigned_worker_id, type, severity, priority, status, lat/lng, before_photos, after_photos, upvote_count, ai_* fields, is_escalated, is_blocked, is_deleted |
| **IssueComment** | `issue_comments` | issue_id, author_id, body, created_at |
| **IssueVote** | `issue_votes` | issue_id, user_id (unique together) |
| **IssueFlag** | `issue_flags` | reporter_id, issue_id, comment_id, reason, status (pending/reviewed/dismissed) |
| **Announcement** | `announcements` | title, body, scope, ward_id, taluka_id, district_id, expires_at, author_id |
| **District** | `districts` | name, state_name |
| **Taluka** | `talukas` | name, district_id |
| **Ward** | `wards` | name, ward_number, taluka_id |
| **RewardTransaction** | `reward_transactions` | user_id, points, event_type, reference_id, note |
| **UserBadge** | `user_badges` | user_id, badge_key, earned_at |
| **WardSubscription** | `ward_subscriptions` | user_id, ward_id (unique together) |
| **WorkerShift** | `worker_shifts` | worker_id, day_of_week, start_time, end_time, is_active |
| **Notification** | `notifications` | user_id, issue_id, title, body, type, is_read |
| **OTP** | `otps` | phone, code, expires_at, is_used |
| **RefreshToken** | `refresh_tokens` | user_id, token_hash (SHA-256), expires_at, is_revoked |

---

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "describe your change"

# Rollback one step
alembic downgrade -1
```

---

## Running Tests

```bash
# Start the server first (in another terminal)
python run.py

# Then run the integration suite
python test_api.py
```

---

## Logs

Written to `logs/` (auto-created, git-ignored):

| File | Contents |
|------|----------|
| `civic_YYYY-MM-DD.log` | All requests + info logs |
| `errors_YYYY-MM-DD.log` | Errors only |

Every HTTP request is logged with method, path, status code, and duration (ms).
