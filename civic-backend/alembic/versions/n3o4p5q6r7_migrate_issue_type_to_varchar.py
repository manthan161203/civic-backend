"""migrate issue_type from ENUM to VARCHAR to support custom issue type slugs

Revision ID: n3o4p5q6r7
Revises: m2n3o4p5q6
Create Date: 2026-04-05

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "n3o4p5q6r7"
down_revision = "m2n3o4p5q6"
branch_labels = None
depends_on = None


def upgrade():
    """
    Convert issue_type from ENUM to VARCHAR.
    Built-in types (garbage, pothole, etc.) remain valid.
    Approved custom issue types are stored as their slugs.
    """
    # 1. Create a VARCHAR column with a temporary name
    op.add_column(
        "issues",
        sa.Column("issue_type_new", sa.String(100), nullable=True),
    )

    # 2. Copy data from old enum column to new varchar column
    # (ENUM values are stored as strings, so direct cast works)
    op.execute(
        "UPDATE issues SET issue_type_new = issue_type::text"
    )

    # 3. Make the new column NOT NULL
    op.alter_column("issues", "issue_type_new", nullable=False)

    # 4. Drop the old enum column
    op.drop_column("issues", "issue_type")

    # 5. Rename the new column to issue_type
    op.execute("ALTER TABLE issues RENAME COLUMN issue_type_new TO issue_type")

    # 6. Drop the old enum type if it exists (safe to do after data migration)
    op.execute("DROP TYPE IF EXISTS issue_type CASCADE")


def downgrade():
    """
    Revert to ENUM-based issue_type.
    """
    # 1. Recreate the enum type with all valid values
    op.execute(
        """
        CREATE TYPE issue_type AS ENUM (
            'garbage', 'pothole', 'streetlight', 'drain', 'water', 'other'
        )
        """
    )

    # 2. Create temporary new column with enum type
    op.add_column(
        "issues",
        sa.Column(
            "issue_type_old",
            postgresql.ENUM(
                "garbage", "pothole", "streetlight", "drain", "water", "other",
                name="issue_type"
            ),
            nullable=True,
        ),
    )

    # 3. Copy data back, converting unknown values to 'other'
    op.execute(
        """
        UPDATE issues SET issue_type_old = CASE
            WHEN issue_type IN ('garbage', 'pothole', 'streetlight', 'drain', 'water')
            THEN issue_type::issue_type
            ELSE 'other'::issue_type
        END
        """
    )

    # 4. Make the column NOT NULL
    op.alter_column("issues", "issue_type_old", nullable=False)

    # 5. Drop old varchar column and rename new enum column
    op.drop_column("issues", "issue_type")
    op.execute("ALTER TABLE issues RENAME COLUMN issue_type_old TO issue_type")
