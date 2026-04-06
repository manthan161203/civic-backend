# Security: Managing Environment Variables & Secrets

This document explains how to properly manage sensitive credentials in the Civic frontend applications.

## ⚠️ Important: Never Commit Secrets

**DO NOT** commit `.env`, `.env.local`, or any files containing API keys, tokens, or credentials to version control.

Files like `.env`, `.env.local`, and credential files are in `.gitignore` and should never be pushed to git.

## Setup Instructions

### Mobile App (Expo)

1. **Copy the template:**
   ```bash
   cd civic-frontend/mobile
   cp .env.example .env
   ```

2. **Fill in your credentials:**
   ```bash
   # .env file
   EXPO_PUBLIC_API_URL=http://your-api-url:8000
   EXPO_PUBLIC_GOOGLE_MAPS_API_KEY=AIzaSy...  # Get from Google Cloud Console
   EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID=...       # OAuth Web Client ID
   EXPO_PUBLIC_GOOGLE_iOS_CLIENT_ID=...       # OAuth iOS Client ID
   EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID=...   # OAuth Android Client ID
   ```

3. **Run the app:**
   ```bash
   npm start
   ```

**Note:** The `.env` file is loaded automatically by Expo and is NOT tracked by git.

### Admin Dashboard (Next.js)

1. **Copy the template:**
   ```bash
   cd civic-frontend/admin
   cp .env.example .env.local
   ```

2. **Fill in your credentials:**
   ```bash
   # .env.local file
   NEXT_PUBLIC_API_URL=http://localhost:8000
   NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=AIzaSy...  # Get from Google Cloud Console
   ```

3. **Run the dashboard:**
   ```bash
   npm run dev
   ```

**Note:** Next.js looks for `.env.local` for local development and this file is NOT tracked by git.

## Getting Google API Keys

### Google Maps API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (if you don't have one)
3. Enable these APIs:
   - Maps SDK for Android
   - Maps SDK for iOS
   - Maps JavaScript API
4. Go to Credentials → Create API Key
5. Restrict the key to your app bundle IDs and packages
6. Copy the key to `.env` or `.env.local`

### Google OAuth Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → Credentials
2. Create OAuth 2.0 Client IDs for:
   - **Web** (for your web app)
   - **Android** (for Android app)
   - **iOS** (for iOS app)
3. For each, you'll get a Client ID
4. Copy them to your `.env` or `.env.local` files

## Environment Variable Naming Convention

- **Public variables** (visible in client code): Prefix with `EXPO_PUBLIC_` (mobile) or `NEXT_PUBLIC_` (admin)
- **Private variables** (server-only): No prefix

Since we're building client-side apps, all our variables are public prefixed but are still managed securely by not committing them.

## What's in .gitignore

Both frontend apps have proper `.gitignore` entries:

**Mobile** (`mobile/.gitignore`):
```
.env
.env.local
.env.*.local
```

**Admin** (`admin/.gitignore`):
```
.env*
```

## Git History Cleanup

If you accidentally committed a secret:

```bash
# Remove from git history (use this carefully!)
git rm --cached .env
git commit -m "chore: remove .env from tracking"
git push

# Then rotate your keys/tokens
```

## CI/CD Deployment

For production deployment (GitHub Actions, EAS, etc.):

1. **Add secrets to your CI/CD provider:**
   - GitHub Actions: Settings → Secrets
   - EAS: `eas.json` → secrets
   - Vercel: Project Settings → Environment Variables

2. **Reference them in your CI/CD pipeline:**
   ```yaml
   env:
     EXPO_PUBLIC_GOOGLE_MAPS_API_KEY: ${{ secrets.GOOGLE_MAPS_API_KEY }}
   ```

## Backend Security

The backend (`civic-backend`) already has proper security setup:
- `.env` files are in `.gitignore`
- Credentials folder is ignored
- Uses environment variables for sensitive data

See `civic-backend/.gitignore` for details.

## Checklist

- ✅ No `.env` files committed to git
- ✅ Using `.env.example` templates
- ✅ `.gitignore` properly configured
- ✅ Environment variables referenced in code
- ✅ API keys rotated when necessary
- ✅ Team members have setup instructions
- ✅ CI/CD secrets configured separately

---

**Questions?** Check the specific README files in `mobile/` and `admin/` directories.
