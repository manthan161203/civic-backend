"""hierarchy and new features

Add admin hierarchy (District/Taluka/Ward location models), sub-admin roles,
department routing, worker availability, issue priority, upvote_count, and new
feature tables: issue_votes, issue_flags, announcements, worker_shifts, ward_subscriptions.

Revision ID: b1c2d3e4f5a6
Revises: c3d4e5f6a7b8
Create Date: 2026-03-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b1c2d3e4f5a6"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. New ENUM types ─────────────────────────────────────────────────────

    # Add new values to existing user_role enum
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'ward_admin'")
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'taluka_admin'")
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'district_admin'")

    # New enum types
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE issue_priority AS ENUM ('urgent', 'high', 'medium', 'low');
        EXCEPTION WHEN duplicate_object THEN null; END $$
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE announcement_scope AS ENUM ('ward', 'taluka', 'district', 'state');
        EXCEPTION WHEN duplicate_object THEN null; END $$
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE flag_reason AS ENUM ('spam', 'inappropriate', 'duplicate', 'false_report', 'other');
        EXCEPTION WHEN duplicate_object THEN null; END $$
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE flag_status AS ENUM ('pending', 'reviewed', 'dismissed');
        EXCEPTION WHEN duplicate_object THEN null; END $$
    """)

    # ── 2. Location tables ────────────────────────────────────────────────────

    op.create_table(
        "districts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("state_name", sa.String(100), nullable=False, server_default="Gujarat"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "talukas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("district_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("districts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "wards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("ward_number", sa.Integer(), nullable=False),
        sa.Column("taluka_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("talukas.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── 3. Users — new columns ────────────────────────────────────────────────

    op.add_column("users", sa.Column("ward_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("wards.id", ondelete="SET NULL"), nullable=True))
    op.add_column("users", sa.Column("taluka_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True))
    op.add_column("users", sa.Column("district_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("districts.id", ondelete="SET NULL"), nullable=True))
    op.add_column("users", sa.Column("department", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("is_available", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")))

    op.create_index("ix_users_ward_id", "users", ["ward_id"])
    op.create_index("ix_users_taluka_id", "users", ["taluka_id"])
    op.create_index("ix_users_district_id", "users", ["district_id"])

    # ── 4. Issues — new columns ───────────────────────────────────────────────

    op.add_column("issues", sa.Column("priority",
                  postgresql.ENUM("urgent", "high", "medium", "low", name="issue_priority", create_type=False),
                  nullable=False, server_default="medium"))
    op.add_column("issues", sa.Column("department", sa.String(50), nullable=True))
    op.add_column("issues", sa.Column("upvote_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("issues", sa.Column("ward_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("wards.id", ondelete="SET NULL"), nullable=True))

    op.create_index("ix_issues_ward_id", "issues", ["ward_id"])
    op.create_index("ix_issues_department", "issues", ["department"])
    op.create_index("ix_issues_reporter_id", "issues", ["reporter_id"])
    op.create_index("ix_issues_assigned_worker_id", "issues", ["assigned_worker_id"])
    op.create_index("ix_issues_status", "issues", ["status"])
    op.create_index("ix_issues_ward", "issues", ["ward"])
    op.create_index("ix_issues_created_at", "issues", ["created_at"])

    # ── 5. Feature tables ─────────────────────────────────────────────────────

    # Issue votes (upvotes)
    op.create_table(
        "issue_votes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("issue_id", "user_id", name="uq_issue_vote"),
    )

    # Announcements
    op.create_table(
        "announcements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("scope",
                  postgresql.ENUM("ward", "taluka", "district", "state", name="announcement_scope", create_type=False),
                  nullable=False),
        sa.Column("ward_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("wards.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("taluka_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False, index=True),
    )

    # Issue flags (content moderation)
    op.create_table(
        "issue_flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("issue_comments.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("reason",
                  postgresql.ENUM("spam", "inappropriate", "duplicate", "false_report", "other", name="flag_reason", create_type=False),
                  nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status",
                  postgresql.ENUM("pending", "reviewed", "dismissed", name="flag_status", create_type=False),
                  nullable=False, server_default="pending", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Worker shifts
    op.create_table(
        "worker_shifts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("worker_id", "day_of_week", name="uq_worker_shift_day"),
    )

    # Ward subscriptions
    op.create_table(
        "ward_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("ward_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("wards.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "ward_id", name="uq_ward_subscription"),
    )

    # ── 6. Performance indexes on notifications ───────────────────────────────

    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])

    # ── 7. Refresh tokens — expires_at index ─────────────────────────────────

    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])


def downgrade() -> None:
    # Drop feature tables
    op.drop_table("ward_subscriptions")
    op.drop_table("worker_shifts")
    op.drop_table("issue_flags")
    op.drop_table("announcements")
    op.drop_table("issue_votes")

    # Drop indexes
    op.drop_index("ix_refresh_tokens_expires_at", "refresh_tokens")
    op.drop_index("ix_notifications_created_at", "notifications")
    op.drop_index("ix_notifications_user_id", "notifications")
    op.drop_index("ix_issues_created_at", "issues")
    op.drop_index("ix_issues_ward", "issues")
    op.drop_index("ix_issues_status", "issues")
    op.drop_index("ix_issues_assigned_worker_id", "issues")
    op.drop_index("ix_issues_reporter_id", "issues")
    op.drop_index("ix_issues_department", "issues")
    op.drop_index("ix_issues_ward_id", "issues")

    # Drop issues columns
    op.drop_column("issues", "ward_id")
    op.drop_column("issues", "upvote_count")
    op.drop_column("issues", "department")
    op.drop_column("issues", "priority")

    # Drop users columns
    op.drop_index("ix_users_district_id", "users")
    op.drop_index("ix_users_taluka_id", "users")
    op.drop_index("ix_users_ward_id", "users")
    op.drop_column("users", "is_available")
    op.drop_column("users", "department")
    op.drop_column("users", "district_id")
    op.drop_column("users", "taluka_id")
    op.drop_column("users", "ward_id")

    # Drop location tables
    op.drop_table("wards")
    op.drop_table("talukas")
    op.drop_table("districts")

    # Drop new enum types
    op.execute("DROP TYPE IF EXISTS flag_status")
    op.execute("DROP TYPE IF EXISTS flag_reason")
    op.execute("DROP TYPE IF EXISTS announcement_scope")
    op.execute("DROP TYPE IF EXISTS issue_priority")
    # Note: cannot remove values from PostgreSQL enum — ward_admin/taluka_admin/district_admin remain
