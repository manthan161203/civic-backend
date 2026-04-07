"""align_indexes_and_constraints - Fix unique constraint vs unique index drift

Revision ID: p5q6r7s8t9
Revises: o4p5q6r7s8
Create Date: 2026-04-07

Fixes detected schema drifts:
1. custom_issue_types.slug: separate UniqueConstraint + non-unique index → unique index
2. issue_squads.issue_id: separate UniqueConstraint + non-unique index → unique index
3. satisfaction_surveys.issue_id: separate UniqueConstraint + non-unique index → unique index
4. issues.issue_type: missing index (model has index=True)
"""

from alembic import op

revision = "p5q6r7s8t9"
down_revision = "o4p5q6r7s8"
branch_labels = None
depends_on = None


def upgrade():
    # 1. custom_issue_types.slug: drop separate unique constraint, make index unique
    op.drop_constraint("custom_issue_types_slug_key", "custom_issue_types", type_="unique")
    op.drop_index("ix_custom_issue_types_slug", table_name="custom_issue_types")
    op.create_index("ix_custom_issue_types_slug", "custom_issue_types", ["slug"], unique=True)

    # 2. issue_squads.issue_id: drop separate unique constraint, make index unique
    op.drop_constraint("issue_squads_issue_id_key", "issue_squads", type_="unique")
    op.drop_index("ix_issue_squads_issue_id", table_name="issue_squads")
    op.create_index("ix_issue_squads_issue_id", "issue_squads", ["issue_id"], unique=True)

    # 3. satisfaction_surveys.issue_id: drop separate unique constraint, make index unique
    op.drop_constraint("satisfaction_surveys_issue_id_key", "satisfaction_surveys", type_="unique")
    op.drop_index("ix_satisfaction_surveys_issue_id", table_name="satisfaction_surveys")
    op.create_index("ix_satisfaction_surveys_issue_id", "satisfaction_surveys", ["issue_id"], unique=True)

    # 4. issues.issue_type: add missing index
    op.create_index("ix_issues_issue_type", "issues", ["issue_type"], unique=False)


def downgrade():
    # 4. Remove issue_type index
    op.drop_index("ix_issues_issue_type", table_name="issues")

    # 3. Revert satisfaction_surveys
    op.drop_index("ix_satisfaction_surveys_issue_id", table_name="satisfaction_surveys")
    op.create_index("ix_satisfaction_surveys_issue_id", "satisfaction_surveys", ["issue_id"], unique=False)
    op.create_unique_constraint("satisfaction_surveys_issue_id_key", "satisfaction_surveys", ["issue_id"])

    # 2. Revert issue_squads
    op.drop_index("ix_issue_squads_issue_id", table_name="issue_squads")
    op.create_index("ix_issue_squads_issue_id", "issue_squads", ["issue_id"], unique=False)
    op.create_unique_constraint("issue_squads_issue_id_key", "issue_squads", ["issue_id"])

    # 1. Revert custom_issue_types
    op.drop_index("ix_custom_issue_types_slug", table_name="custom_issue_types")
    op.create_index("ix_custom_issue_types_slug", "custom_issue_types", ["slug"], unique=False)
    op.create_unique_constraint("custom_issue_types_slug_key", "custom_issue_types", ["slug"])
