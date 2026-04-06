"""add disputes, surveys, bookmarks, complaints, custom issue types, and new columns

Revision ID: m2n3o4p5q6
Revises: l1g2h3i4j5
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa

revision = "m2n3o4p5q6"
down_revision = "l1g2h3i4j5"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ── Pure SQL to avoid SQLAlchemy enum auto-creation issues ────────────
    conn.execute(sa.text("""
        DO $$ BEGIN CREATE TYPE dispute_status AS ENUM ('open', 'under_review', 'accepted', 'rejected'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN CREATE TYPE complaint_reason AS ENUM ('rude_behavior', 'poor_work', 'delayed', 'no_show', 'other'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN CREATE TYPE complaint_status AS ENUM ('pending', 'investigating', 'resolved', 'dismissed'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS disputes (
            id UUID PRIMARY KEY,
            issue_id UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            citizen_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            photos JSONB NOT NULL DEFAULT '[]',
            status dispute_status NOT NULL DEFAULT 'open',
            admin_notes TEXT,
            resolved_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_disputes_issue_id ON disputes (issue_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_disputes_citizen_id ON disputes (citizen_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_disputes_status ON disputes (status);"))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS satisfaction_surveys (
            id UUID PRIMARY KEY,
            issue_id UUID NOT NULL UNIQUE REFERENCES issues(id) ON DELETE CASCADE,
            citizen_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            fully_resolved BOOLEAN NOT NULL,
            speed_rating INTEGER NOT NULL,
            would_report_again BOOLEAN NOT NULL,
            feedback TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_satisfaction_surveys_issue_id ON satisfaction_surveys (issue_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_satisfaction_surveys_citizen_id ON satisfaction_surveys (citizen_id);"))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS issue_bookmarks (
            id UUID PRIMARY KEY,
            issue_id UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_issue_bookmark UNIQUE (issue_id, user_id)
        );
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_issue_bookmarks_issue_id ON issue_bookmarks (issue_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_issue_bookmarks_user_id ON issue_bookmarks (user_id);"))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS worker_complaints (
            id UUID PRIMARY KEY,
            worker_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            citizen_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            issue_id UUID REFERENCES issues(id) ON DELETE SET NULL,
            reason complaint_reason NOT NULL,
            description TEXT NOT NULL,
            photos JSONB NOT NULL DEFAULT '[]',
            status complaint_status NOT NULL DEFAULT 'pending',
            admin_notes TEXT,
            resolved_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_worker_complaints_worker_id ON worker_complaints (worker_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_worker_complaints_citizen_id ON worker_complaints (citizen_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_worker_complaints_issue_id ON worker_complaints (issue_id);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_worker_complaints_status ON worker_complaints (status);"))

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS custom_issue_types (
            id UUID PRIMARY KEY,
            label VARCHAR(100) NOT NULL,
            slug VARCHAR(100) NOT NULL UNIQUE,
            suggested_by UUID REFERENCES users(id) ON DELETE SET NULL,
            usage_count INTEGER NOT NULL DEFAULT 1,
            is_approved BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_custom_issue_types_label ON custom_issue_types (label);"))
    conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_custom_issue_types_slug ON custom_issue_types (slug);"))

    # ── New columns on existing tables ───────────────────────────────────
    conn.execute(sa.text("ALTER TABLE issues ADD COLUMN IF NOT EXISTS custom_issue_type_label VARCHAR(100);"))
    conn.execute(sa.text("ALTER TABLE issues ADD COLUMN IF NOT EXISTS escalation_level INTEGER NOT NULL DEFAULT 0;"))
    conn.execute(sa.text("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS action_type VARCHAR(50);"))


def downgrade():
    conn = op.get_bind()

    conn.execute(sa.text("ALTER TABLE notifications DROP COLUMN IF EXISTS action_type;"))
    conn.execute(sa.text("ALTER TABLE issues DROP COLUMN IF EXISTS escalation_level;"))
    conn.execute(sa.text("ALTER TABLE issues DROP COLUMN IF EXISTS custom_issue_type_label;"))

    conn.execute(sa.text("DROP TABLE IF EXISTS custom_issue_types;"))
    conn.execute(sa.text("DROP TABLE IF EXISTS worker_complaints;"))
    conn.execute(sa.text("DROP TABLE IF EXISTS issue_bookmarks;"))
    conn.execute(sa.text("DROP TABLE IF EXISTS satisfaction_surveys;"))
    conn.execute(sa.text("DROP TABLE IF EXISTS disputes;"))

    conn.execute(sa.text("DROP TYPE IF EXISTS complaint_status;"))
    conn.execute(sa.text("DROP TYPE IF EXISTS complaint_reason;"))
    conn.execute(sa.text("DROP TYPE IF EXISTS dispute_status;"))
