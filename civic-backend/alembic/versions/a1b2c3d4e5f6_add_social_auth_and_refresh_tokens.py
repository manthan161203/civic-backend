"""add social auth fields and refresh_tokens table

Revision ID: a1b2c3d4e5f6
Revises: 6478303af1f9
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "6478303af1f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users table: make phone nullable, add social auth columns ────────────
    op.alter_column("users", "phone", nullable=True, existing_type=sa.String())

    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.add_column("users", sa.Column("google_id", sa.String(), nullable=True))
    op.add_column("users", sa.Column("aadhar_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("aadhar_verified", sa.Boolean(), server_default=sa.false(), nullable=False))

    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_google_id", "users", ["google_id"], unique=True)
    op.create_index("ix_users_aadhar_hash", "users", ["aadhar_hash"], unique=True)

    # ── refresh_tokens table ──────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")

    op.drop_index("ix_users_aadhar_hash", table_name="users")
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")

    op.drop_column("users", "aadhar_verified")
    op.drop_column("users", "aadhar_hash")
    op.drop_column("users", "google_id")
    op.drop_column("users", "email")

    op.alter_column("users", "phone", nullable=False, existing_type=sa.String())
