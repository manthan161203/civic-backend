# Civic — Frontend

Mobile and web frontend for the Civic Issue Management Platform.

> This repository is under active development.

## Overview

Civic is a citizen-facing platform for reporting, tracking, and resolving local infrastructure issues (potholes, streetlights, garbage, etc.) with real-time status updates, multilingual support (English / Hindi / Gujarati), and AI-powered classification.

## Getting Started

Prerequisites:
- Node.js (v18+ recommended) and `npm` or `pnpm`
- Git
- For mobile: Expo CLI (optional) — `npm install -g expo-cli` (or use `npx expo`)

### Admin (web)

Path: `admin/` (Next.js)

Commands:

```
cd admin
npm install
npm run dev
```

This starts the Next.js development server at `http://localhost:3000` by default.

### Mobile (Expo)

Path: `mobile/` (Expo / React Native)

Commands:

```
cd mobile
npm install
npm run start       # open Expo Dev Tools
npm run android     # open on Android device/emulator
npm run ios         # open on iOS simulator (macOS only)
```

If you prefer `npx expo`:

```
npx expo start
npx expo run:android
```

## Environment

Set any required API base URLs or keys before running (examples):
- For Next.js: `NEXT_PUBLIC_API_URL` (in `.env.local`)
- For mobile: update the config or `.env` used by the app

## Git: push to `development`

Recommended: create a feature branch and open a Pull Request targeting `development`.

```
# create feature branch
git checkout -b feature/your-short-description
git add .
git commit -m "feat: short description"
git push -u origin feature/your-short-description
# Create a PR on GitHub to merge into 'development'
```

If you must push directly to `development` (not recommended without review):

```
git checkout development
git pull origin development
git merge --no-ff feature/your-short-description
git push origin development
```

Note: some repositories use `develop` instead of `development` — adjust commands accordingly.

## Related

- Backend API: `../civic-backend`
