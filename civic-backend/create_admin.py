#!/usr/bin/env python3
"""
One-time Admin Seeder
=====================
Creates the first admin user account so you can bootstrap the system.

Usage:
    python create_admin.py

You will be prompted for:
    - Phone number  (used to log in via OTP)
    - Name          (display name)
    - Ward          (optional)

Run this ONCE before starting the server for the first time.
If an admin with the same phone already exists, the script exits safely.

Requirements:
    - DATABASE_URL must be set in .env
    - Run from the project root (same directory as this file)
"""

import sys
import os

# Ensure project root is on the path so app imports work
sys.path.insert(0, os.path.dirname(__file__))


def main():
    print("\n=== Civic Backend — First Admin Setup ===\n")

    # ── Collect inputs ────────────────────────────────────────────────────────

    phone = input("Admin phone number (with country code, e.g. +919876543210): ").strip()
    if not phone:
        print("ERROR: Phone number is required.")
        sys.exit(1)

    import re
    if not re.match(r"^\+?[1-9]\d{9,14}$", phone):
        print("ERROR: Invalid phone number format. Must be 10-15 digits, optionally starting with +")
        sys.exit(1)

    name = input("Admin display name (e.g. Manthan Patel): ").strip() or None
    ward = input("Ward/area (optional, press Enter to skip): ").strip() or None

    # ── Load DB and models ────────────────────────────────────────────────────

    print("\nConnecting to database...")
    try:
        from app.database import SessionLocal, check_db_connection
        from app.models.user import User
        from app.models import __all__ as _  # ensure all models are registered
        from app.services.utils import get_user_by_phone
    except Exception as e:
        print(f"ERROR: Failed to import app modules. Make sure .env is configured.\n  {e}")
        sys.exit(1)

    if not check_db_connection():
        print("ERROR: Cannot connect to database. Check DATABASE_URL in .env")
        sys.exit(1)

    # ── Check for existing admin / duplicate phone ────────────────────────────

    db = SessionLocal()
    try:
        existing = get_user_by_phone(phone, db)
        if existing:
            if existing.role == "admin":
                print(f"\n✓ Admin with phone {phone} already exists (id={existing.id}, name={existing.name}).")
                print("  No changes made. You can log in using OTP.")
            else:
                print(f"\n  User with phone {phone} exists but has role='{existing.role}'.")
                promote = input("  Promote this user to admin? [y/N]: ").strip().lower()
                if promote == "y":
                    existing.role = "admin"
                    if name:
                        existing.name = name
                    if ward:
                        existing.ward = ward
                    db.commit()
                    print(f"\n✓ User {existing.id} promoted to admin.")
                else:
                    print("  No changes made.")
            return

        # ── Create new admin ──────────────────────────────────────────────────

        admin = User(
            phone=phone,
            name=name,
            ward=ward,
            role="admin",
            language="en",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)

        print(f"""
✓ Admin account created successfully!

  ID    : {admin.id}
  Phone : {admin.phone}
  Name  : {admin.name or '(not set)'}
  Ward  : {admin.ward or '(not set)'}
  Role  : {admin.role}

  Next steps:
    1. Start the server:  python run.py
    2. Log in via OTP:    POST /auth/send-otp  →  POST /auth/verify-otp
    3. Open the docs:     http://localhost:<port>/docs
""")

    except Exception as e:
        db.rollback()
        print(f"\nERROR: Failed to create admin account.\n  {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()