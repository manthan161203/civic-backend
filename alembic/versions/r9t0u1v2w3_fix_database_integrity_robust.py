"""Fix CRITICAL database integrity issues: cascading delete & FK constraints (ROBUST)

AUDIT FINDINGS: 10 critical cascading delete issues identified
- 5 missing CASCADE deletes (orphaned records risk)
- 4 missing SET NULL (audit trail preservation)
- 1 schema mismatch (NOT NULL constraint vs SET NULL FK)

WHEN WORKER DELETED:
✅ WorkerShift → CASCADE DELETE
✅ WorkerComplaint (as worker) → CASCADE DELETE  
✅ RewardTransaction → CASCADE DELETE
✅ UserBadge → CASCADE DELETE
✅ RefreshToken → CASCADE DELETE
✅ Issue.assigned_worker → SET NULL
❌ Notification (user_id) → ORPHANED (FIX: CASCADE)
❌ Notification (issue_id) → ORPHANED (FIX: CASCADE)
❌ IssueFlag (reporter_id) → ORPHANED (FIX: CASCADE)
❌ Issue.blocked_by_id → BLOCKS DELETION (FIX: SET NULL)
❌ Issue.unblocked_by_id → BLOCKS DELETION (FIX: SET NULL)
❌ IssueSquad.lead_worker → BLOCKS DELETION (FIX: Make nullable)
❌ Geofence.created_by → ORPHANED (FIX: SET NULL)
❌ Announcement.author → ORPHANED (FIX: CASCADE)
❌ WorkerComplaint.resolved_by → BLOCKS DELETION (FIX: SET NULL)
❌ Dispute.resolved_by → BLOCKS DELETION (FIX: SET NULL)

Revision ID: r9t0u1v2w3
Revises: r7s8t9u0v1
Create Date: 2026-04-08

SUPERSEDED — this revision's DDL is now a no-op.
==============================================
Three revisions were authored against the same parent (``r7s8t9u0v1``), each
applying this same set of ten fixes: this one, ``r8s9t0u1v2``, and
``r0u1v2w3x4`` (v3). ``merge_integrity_heads`` rejoins them, so all three run
on every database — and re-applying the DDL was actively broken:

  * This revision names the FKs ``issues_blocked_by_id_fkey`` while v3 names
    them ``fk_issues_blocked_by_id``, so a second re-application would leave
    duplicate foreign keys on the same columns.
  * Each fix wrapped its ``DROP CONSTRAINT`` in a bare ``except: pass``. In
    Postgres a failed statement aborts the whole transaction, and catching the
    Python exception does not roll it back — so once the first DROP failed,
    every later statement (including Alembic's own INSERT into
    ``alembic_version``) failed with ``InFailedSqlTransaction`` and the
    migration could never complete.

v3 is the canonical implementation and uses the constraint names that actually
exist, so the fixes are applied there. This file is kept — rather than deleted —
so that databases which already recorded this revision keep a valid history.
"""
from alembic import op
import sqlalchemy as sa

revision = 'r9t0u1v2w3'
down_revision = 'r7s8t9u0v1'
branch_labels = None
depends_on = None


def upgrade():
    """No-op. Superseded by revision ``r0u1v2w3x4`` (v3).

    See the module docstring above for the history. Nothing is executed here.
    """
    pass


def downgrade():
    """No-op. See ``upgrade``."""
    pass
